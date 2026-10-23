/*
 * Cache em disco para os downloads http/https do Flash.
 *
 * O Flash baixa recursos pelo urlmon. O cache do WinInet no Wine grava os
 * arquivos mas não os reaproveita, então o jogo baixava tudo a cada sessão.
 * Registramos, só neste processo, um tratador de protocolo para http e https
 * (IInternetSession::RegisterNameSpace):
 *
 *   - acerto: entrega o arquivo do disco ao urlmon, como o protocolo file;
 *   - falha: repassa ao tratador HTTP(S) padrão do urlmon e copia os bytes
 *     lidos; com status 200, grava no disco ao final.
 *
 * Regras (as mesmas do cache do Ruffle no cliente Android):
 *   - nomes com hash de conteúdo (nome_1a2b3c4d.ext) nunca mudam: sempre válidos,
 *     e valem para todos os servidores do mesmo domínio;
 *   - os demais valem por 10% do tempo desde Last-Modified, até 7 dias;
 *     depois disso são revalidados com If-Modified-Since: no 304, o arquivo
 *     do disco é entregue e volta a valer;
 *   - só recursos estáticos (pela extensão) e nunca pedidos com "rnd=",
 *     exceto nomes com hash.
 */
#define COBJMACROS
#include <windows.h>
#include <urlmon.h>
#include <wininet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <ctype.h>

#include "http_cache.h"

#define MAX_HEURISTIC_FRESHNESS (7 * 24 * 60 * 60)
static const char *const static_extensions[] = {
    "swf", "bswf", "spl", "png", "jpg", "jpeg", "gif", "bmp", "mp3", "wav",
    "xml", "dat", "zip", "txt", "ttf", "otf", NULL,
};
static const char header_magic[] = "legend-http-cache-v1";

static char cache_dir[1024];

/* {6f1c2a54-7d3e-4b8a-9a61-2e5d0c7b9f10}: só identifica o tratador no registro da sessão. */
static const CLSID CLSID_LegendHttpCache =
    {0x6f1c2a54, 0x7d3e, 0x4b8a, {0x9a, 0x61, 0x2e, 0x5d, 0x0c, 0x7b, 0x9f, 0x10}};

/* ---- Regras e armazenamento ---- */

static unsigned long long fnv1a(const char *text)
{
    unsigned long long hash = 0xcbf29ce484222325ULL;
    for (; *text; text++) hash = (hash ^ (unsigned char)*text) * 0x100000001b3ULL;
    return hash;
}

static BOOL ends_with_hash(const char *stem, size_t len)
{
    size_t i = len, digits = 0;
    while (i > 0 && isxdigit((unsigned char)stem[i - 1])) { i--; digits++; }
    return digits >= 8 && i > 0 && stem[i - 1] == '_';
}

/*
 * Calcula a chave do cache para uma URL. Retorna FALSE quando a URL não deve
 * ser cacheada; *immutable indica nome com hash de conteúdo.
 */
static BOOL cache_key(const char *url, char *key, size_t size, BOOL *immutable)
{
    const char *query, *path_end, *slash, *dot;
    char extension[8];
    size_t i, path_len;

    if (strncmp(url, "http://", 7) && strncmp(url, "https://", 8)) return FALSE;
    query = strchr(url, '?');
    path_end = query ? query : url + strlen(url);
    path_len = path_end - url;
    for (slash = path_end; slash > url && slash[-1] != '/'; slash--);
    for (dot = path_end; dot > slash && dot[-1] != '.'; dot--);
    if (dot == slash || (size_t)(path_end - dot) >= sizeof(extension)) return FALSE;
    for (i = 0; dot + i < path_end; i++) extension[i] = (char)tolower((unsigned char)dot[i]);
    extension[i] = 0;
    for (i = 0; static_extensions[i] && strcmp(static_extensions[i], extension); i++);
    if (!static_extensions[i]) return FALSE;

    *immutable = ends_with_hash(slash, (dot - 1) - slash);
    if (*immutable) {
        /*
         * O hash identifica o conteúdo: esquema e query não importam, e os
         * servidores do mesmo domínio (res1265oas, res1217oas...) compartilham
         * a entrada, pois servem os mesmos arquivos.
         */
        const char *host = strstr(url, "://") + 3, *host_end = host, *domain, *path;
        int dots = 0;
        while (*host_end && *host_end != '/' && *host_end != '?' && *host_end != ':') host_end++;
        for (domain = host_end; domain > host && !(domain[-1] == '.' && ++dots == 2); domain--);
        if (strspn(host, "0123456789.") >= (size_t)(host_end - host)) domain = host; /* IP inteiro */
        for (path = host_end; *path && *path != '/' && *path != '?'; path++);
        if (snprintf(key, size, "immutable:%.*s%.*s", (int)(host_end - domain), domain,
                     (int)(path_end - path), path) >= (int)size)
            return FALSE;
        return TRUE;
    }
    if (query && (strstr(query, "?rnd=") || strstr(query, "&rnd="))) return FALSE;
    if (query && !query[1]) path_len = query - url; /* "x.png?" e "x.png" são o mesmo pedido */
    else path_len = strlen(url);
    if (path_len >= size) return FALSE;
    memcpy(key, url, path_len);
    key[path_len] = 0;
    return TRUE;
}

static void entry_path(const char *key, char *path, size_t size)
{
    snprintf(path, size, "%s/%016llx", cache_dir, fnv1a(key));
}

struct entry {
    char content_type[128];
    long long last_modified;
    long long stored_at;
    int immutable;
    BYTE *body;
    DWORD size;
};

/* Lê uma entrada; a chave gravada é conferida para descartar colisões de hash. */
static BOOL read_entry(const char *key, struct entry *entry)
{
    char path[1200], line[4096];
    long start, end;
    FILE *file;

    entry_path(key, path, sizeof(path));
    if (!(file = fopen(path, "rb"))) return FALSE;
    memset(entry, 0, sizeof(*entry));
    if (!fgets(line, sizeof(line), file) || strncmp(line, header_magic, strlen(header_magic)) ||
        !fgets(line, sizeof(line), file) || (line[strcspn(line, "\n")] = 0, strcmp(line, key)) ||
        !fgets(entry->content_type, sizeof(entry->content_type), file) ||
        fscanf(file, "%lld\n%lld\n%d\n", &entry->last_modified, &entry->stored_at,
               &entry->immutable) != 3) {
        fclose(file);
        return FALSE;
    }
    entry->content_type[strcspn(entry->content_type, "\n")] = 0;
    start = ftell(file);
    fseek(file, 0, SEEK_END);
    end = ftell(file);
    fseek(file, start, SEEK_SET);
    entry->size = end - start;
    entry->body = malloc(entry->size ? entry->size : 1);
    if (fread(entry->body, 1, entry->size, file) != entry->size) {
        free(entry->body);
        fclose(file);
        return FALSE;
    }
    fclose(file);
    return TRUE;
}

static BOOL entry_is_fresh(const struct entry *entry, long long now)
{
    long long age, lifetime;
    if (entry->immutable) return TRUE;
    if (entry->last_modified <= 0) return FALSE;
    age = entry->stored_at - entry->last_modified;
    lifetime = age > 0 ? age / 10 : 0;
    if (lifetime > MAX_HEURISTIC_FRESHNESS) lifetime = MAX_HEURISTIC_FRESHNESS;
    return now < entry->stored_at + lifetime;
}

static void write_entry(const char *key, const char *content_type, long long last_modified,
                        BOOL immutable, const BYTE *body, DWORD size)
{
    char path[1200], temp[1300];
    FILE *file;

    if (!immutable && last_modified <= 0) return; /* sem como saber quando expira */
    entry_path(key, path, sizeof(path));
    snprintf(temp, sizeof(temp), "%s.tmp", path);
    if (!(file = fopen(temp, "wb"))) return;
    fprintf(file, "%s\n%s\n%s\n%lld\n%lld\n%d\n", header_magic, key,
            content_type ? content_type : "", last_modified, (long long)time(NULL), immutable);
    if (fwrite(body, 1, size, file) != size) {
        fclose(file);
        remove(temp);
        return;
    }
    fclose(file);
    rename(temp, path); /* leitores nunca veem uma entrada pela metade */
}

static char *to_utf8(const WCHAR *text)
{
    int n = WideCharToMultiByte(CP_UTF8, 0, text, -1, NULL, 0, NULL, NULL);
    char *out = malloc(n);
    WideCharToMultiByte(CP_UTF8, 0, text, -1, out, n, NULL, NULL);
    return out;
}

/* ---- Tratador de protocolo ---- */

struct protocol {
    IInternetProtocol IInternetProtocol_iface;
    IInternetProtocolSink IInternetProtocolSink_iface; /* entregue ao tratador de rede */
    IServiceProvider IServiceProvider_iface;           /* idem, para acrescentar cabeçalhos */
    IHttpNegotiate IHttpNegotiate_iface;
    LONG ref;
    IInternetProtocolSink *sink;  /* do urlmon */
    IInternetProtocol *network;   /* tratador HTTP(S) padrão, em caso de falha */
    IHttpNegotiate *negotiate;    /* do urlmon, envolvido por IHttpNegotiate_iface */
    char key[2048];
    BOOL cacheable, immutable, from_cache, stored;
    struct entry stale;           /* entrada vencida, revalidada com o servidor */
    BOOL have_stale, revalidated, served;
    DWORD started;                /* GetTickCount() no Start, para o log */
    char content_type[128];
    BYTE *data;                   /* acerto: corpo inteiro; falha: bytes já lidos */
    DWORD size, capacity, position;
};

static struct protocol *impl_from_protocol(IInternetProtocol *iface)
{
    return CONTAINING_RECORD(iface, struct protocol, IInternetProtocol_iface);
}

static struct protocol *impl_from_sink(IInternetProtocolSink *iface)
{
    return CONTAINING_RECORD(iface, struct protocol, IInternetProtocolSink_iface);
}

static HRESULT WINAPI protocol_QueryInterface(IInternetProtocol *iface, REFIID riid, void **out)
{
    struct protocol *This = impl_from_protocol(iface);
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IInternetProtocolRoot) ||
        IsEqualIID(riid, &IID_IInternetProtocol)) {
        *out = iface;
        IInternetProtocol_AddRef(iface);
        return S_OK;
    }
    /*
     * Status e cabeçalhos HTTP (IWinInetHttpInfo) vêm do tratador de rede;
     * depois de um 304 eles não descrevem o corpo entregue, como num acerto.
     */
    if (This->network && !This->revalidated) return IInternetProtocol_QueryInterface(This->network, riid, out);
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI protocol_AddRef(IInternetProtocol *iface)
{
    return InterlockedIncrement(&impl_from_protocol(iface)->ref);
}

static ULONG WINAPI protocol_Release(IInternetProtocol *iface)
{
    struct protocol *This = impl_from_protocol(iface);
    ULONG ref = InterlockedDecrement(&This->ref);
    if (!ref) {
        if (This->network) IInternetProtocol_Release(This->network);
        if (This->negotiate) IHttpNegotiate_Release(This->negotiate);
        if (This->sink) IInternetProtocolSink_Release(This->sink);
        if (This->have_stale) free(This->stale.body);
        free(This->data);
        free(This);
    }
    return ref;
}

static long long query_last_modified(IInternetProtocol *network)
{
    IWinInetHttpInfo *info;
    SYSTEMTIME st;
    FILETIME ft;
    DWORD size = sizeof(st), flags = 0;
    long long result = 0;
    if (FAILED(IInternetProtocol_QueryInterface(network, &IID_IWinInetHttpInfo, (void **)&info)))
        return 0;
    if (SUCCEEDED(IWinInetHttpInfo_QueryInfo(info, HTTP_QUERY_LAST_MODIFIED | HTTP_QUERY_FLAG_SYSTEMTIME,
                                             &st, &size, &flags, NULL)) &&
        SystemTimeToFileTime(&st, &ft)) {
        ULONGLONG ticks = ((ULONGLONG)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
        result = (long long)((ticks - 116444736000000000ULL) / 10000000ULL);
    }
    IWinInetHttpInfo_Release(info);
    return result;
}

static DWORD query_status(IInternetProtocol *network)
{
    IWinInetHttpInfo *info;
    DWORD status = 0, size = sizeof(status), flags = 0;
    if (FAILED(IInternetProtocol_QueryInterface(network, &IID_IWinInetHttpInfo, (void **)&info)))
        return 0;
    if (FAILED(IWinInetHttpInfo_QueryInfo(info, HTTP_QUERY_STATUS_CODE | HTTP_QUERY_FLAG_NUMBER,
                                          &status, &size, &flags, NULL)))
        status = 0;
    IWinInetHttpInfo_Release(info);
    return status;
}

/* Entrega ao urlmon o corpo que está em This->data, como o protocolo file. */
static void serve_cached(struct protocol *This)
{
    WCHAR mime[128];
    This->from_cache = TRUE;
    This->position = 0;
    MultiByteToWideChar(CP_UTF8, 0, This->content_type, -1, mime, ARRAYSIZE(mime));
    if (mime[0]) {
        IInternetProtocolSink_ReportProgress(This->sink, BINDSTATUS_MIMETYPEAVAILABLE, mime);
        IInternetProtocolSink_ReportProgress(This->sink, BINDSTATUS_VERIFIEDMIMETYPEAVAILABLE, mime);
    }
    IInternetProtocolSink_ReportData(This->sink, BSCF_FIRSTDATANOTIFICATION | BSCF_LASTDATANOTIFICATION |
                                     BSCF_DATAFULLYAVAILABLE, This->size, This->size);
    IInternetProtocolSink_ReportResult(This->sink, S_OK, 0, NULL);
}

/* O servidor respondeu 304: entrega a entrada vencida e a marca como válida de novo. */
static void serve_revalidated(struct protocol *This)
{
    if (This->served) return;
    This->served = TRUE;
    fprintf(stderr, "flashhost: cache 304 ms=%u %s\n", (unsigned)(GetTickCount() - This->started), This->key);
    free(This->data);
    This->data = This->stale.body;
    This->size = This->stale.size;
    This->have_stale = FALSE; /* o corpo agora pertence a This->data */
    snprintf(This->content_type, sizeof(This->content_type), "%s", This->stale.content_type);
    write_entry(This->key, This->content_type, This->stale.last_modified, FALSE, This->data, This->size);
    serve_cached(This);
}

static HRESULT WINAPI protocol_Start(IInternetProtocol *iface, LPCWSTR url, IInternetProtocolSink *sink,
                                     IInternetBindInfo *bind_info, DWORD flags, HANDLE_PTR reserved)
{
    struct protocol *This = impl_from_protocol(iface);
    struct entry entry;
    char *utf8 = to_utf8(url);
    HRESULT hr;

    This->sink = sink;
    IInternetProtocolSink_AddRef(sink);
    This->started = GetTickCount();
    This->cacheable = cache_key(utf8, This->key, sizeof(This->key), &This->immutable);
    if (This->cacheable && read_entry(This->key, &entry)) {
        if (entry_is_fresh(&entry, time(NULL))) {
            fprintf(stderr, "flashhost: cache acerto %s\n", This->key);
            free(utf8);
            This->data = entry.body;
            This->size = entry.size;
            snprintf(This->content_type, sizeof(This->content_type), "%s", entry.content_type);
            serve_cached(This);
            return S_OK;
        }
        This->stale = entry;
        This->have_stale = TRUE;
    }

    fprintf(stderr, "flashhost: cache %s %s\n",
            This->have_stale ? "revalidar" : This->cacheable ? "falha" : "ignorado", utf8);
    hr = CoCreateInstance(strncmp(utf8, "https:", 6) ? &CLSID_HttpProtocol : &CLSID_HttpSProtocol,
                          NULL, CLSCTX_INPROC_SERVER, &IID_IInternetProtocol, (void **)&This->network);
    free(utf8);
    if (FAILED(hr)) {
        fprintf(stderr, "flashhost: tratador HTTP padrão indisponível (0x%08x)\n", (unsigned)hr);
        return hr;
    }
    return IInternetProtocol_Start(This->network, url, &This->IInternetProtocolSink_iface, bind_info,
                                   flags, reserved);
}

static HRESULT WINAPI protocol_Continue(IInternetProtocol *iface, PROTOCOLDATA *data)
{
    struct protocol *This = impl_from_protocol(iface);
    return This->network ? IInternetProtocol_Continue(This->network, data) : S_OK;
}

static HRESULT WINAPI protocol_Abort(IInternetProtocol *iface, HRESULT reason, DWORD options)
{
    struct protocol *This = impl_from_protocol(iface);
    return This->network ? IInternetProtocol_Abort(This->network, reason, options) : S_OK;
}

static HRESULT WINAPI protocol_Terminate(IInternetProtocol *iface, DWORD options)
{
    struct protocol *This = impl_from_protocol(iface);
    return This->network ? IInternetProtocol_Terminate(This->network, options) : S_OK;
}

static HRESULT WINAPI protocol_Suspend(IInternetProtocol *iface)
{
    struct protocol *This = impl_from_protocol(iface);
    return This->network ? IInternetProtocol_Suspend(This->network) : E_NOTIMPL;
}

static HRESULT WINAPI protocol_Resume(IInternetProtocol *iface)
{
    struct protocol *This = impl_from_protocol(iface);
    return This->network ? IInternetProtocol_Resume(This->network) : E_NOTIMPL;
}

static HRESULT WINAPI protocol_Read(IInternetProtocol *iface, void *buffer, ULONG size, ULONG *read)
{
    struct protocol *This = impl_from_protocol(iface);
    HRESULT hr;

    if (This->from_cache) {
        ULONG count = min(size, This->size - This->position);
        memcpy(buffer, This->data + This->position, count);
        This->position += count;
        if (read) *read = count;
        return count ? S_OK : S_FALSE;
    }

    hr = IInternetProtocol_Read(This->network, buffer, size, read);
    if (This->cacheable && read && *read) {
        if (This->size + *read > This->capacity) {
            This->capacity = max(This->capacity * 2, This->size + *read);
            This->data = realloc(This->data, This->capacity);
        }
        memcpy(This->data + This->size, buffer, *read);
        This->size += *read;
    }
    if (hr == S_FALSE && This->cacheable && !This->stored) {
        /* Fim dos dados: grava a resposta completa se veio com status 200. */
        This->stored = TRUE;
        DWORD status = query_status(This->network);
        long long last_modified = query_last_modified(This->network);
        fprintf(stderr, "flashhost: cache fim status=%u bytes=%u ms=%u last-modified=%lld %s\n",
                (unsigned)status, (unsigned)This->size, (unsigned)(GetTickCount() - This->started),
                last_modified, This->key);
        if (status == 200)
            write_entry(This->key, This->content_type, last_modified, This->immutable, This->data,
                        This->size);
    }
    return hr;
}

static HRESULT WINAPI protocol_Seek(IInternetProtocol *iface, LARGE_INTEGER move, DWORD origin,
                                    ULARGE_INTEGER *position)
{
    return E_NOTIMPL;
}

static HRESULT WINAPI protocol_LockRequest(IInternetProtocol *iface, DWORD options)
{
    struct protocol *This = impl_from_protocol(iface);
    return This->network ? IInternetProtocol_LockRequest(This->network, options) : S_OK;
}

static HRESULT WINAPI protocol_UnlockRequest(IInternetProtocol *iface)
{
    struct protocol *This = impl_from_protocol(iface);
    return This->network ? IInternetProtocol_UnlockRequest(This->network) : S_OK;
}

static IInternetProtocolVtbl protocol_vtbl = {
    protocol_QueryInterface, protocol_AddRef, protocol_Release, protocol_Start,
    protocol_Continue, protocol_Abort, protocol_Terminate, protocol_Suspend, protocol_Resume,
    protocol_Read, protocol_Seek, protocol_LockRequest, protocol_UnlockRequest,
};

/* Sink entregue ao tratador de rede: repassa tudo ao urlmon e guarda o MIME. */

static HRESULT WINAPI psink_QueryInterface(IInternetProtocolSink *iface, REFIID riid, void **out)
{
    struct protocol *This = impl_from_sink(iface);
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IInternetProtocolSink)) {
        *out = iface;
        IInternetProtocolSink_AddRef(iface);
        return S_OK;
    }
    /* O tratador de rede pega o IHttpNegotiate por aqui (cabeçalhos condicionais). */
    if (This->have_stale && IsEqualIID(riid, &IID_IServiceProvider)) {
        *out = &This->IServiceProvider_iface;
        IServiceProvider_AddRef(&This->IServiceProvider_iface);
        return S_OK;
    }
    return IInternetProtocolSink_QueryInterface(This->sink, riid, out);
}

static ULONG WINAPI psink_AddRef(IInternetProtocolSink *iface)
{
    return protocol_AddRef(&impl_from_sink(iface)->IInternetProtocol_iface);
}

static ULONG WINAPI psink_Release(IInternetProtocolSink *iface)
{
    return protocol_Release(&impl_from_sink(iface)->IInternetProtocol_iface);
}

static HRESULT WINAPI psink_Switch(IInternetProtocolSink *iface, PROTOCOLDATA *data)
{
    return IInternetProtocolSink_Switch(impl_from_sink(iface)->sink, data);
}

static HRESULT WINAPI psink_ReportProgress(IInternetProtocolSink *iface, ULONG status, LPCWSTR text)
{
    struct protocol *This = impl_from_sink(iface);
    if (This->revalidated && (status == BINDSTATUS_MIMETYPEAVAILABLE ||
                              status == BINDSTATUS_VERIFIEDMIMETYPEAVAILABLE))
        return S_OK; /* o MIME do 304 não é o do corpo; serve_cached informa o certo */
    if (text && (status == BINDSTATUS_MIMETYPEAVAILABLE || status == BINDSTATUS_VERIFIEDMIMETYPEAVAILABLE))
        WideCharToMultiByte(CP_UTF8, 0, text, -1, This->content_type, sizeof(This->content_type), NULL, NULL);
    return IInternetProtocolSink_ReportProgress(This->sink, status, text);
}

static HRESULT WINAPI psink_ReportData(IInternetProtocolSink *iface, DWORD flags, ULONG progress, ULONG max)
{
    struct protocol *This = impl_from_sink(iface);
    if (This->revalidated) {
        serve_revalidated(This);
        return S_OK;
    }
    return IInternetProtocolSink_ReportData(This->sink, flags, progress, max);
}

static HRESULT WINAPI psink_ReportResult(IInternetProtocolSink *iface, HRESULT result, DWORD error,
                                         LPCWSTR text)
{
    struct protocol *This = impl_from_sink(iface);
    if (This->revalidated) {
        serve_revalidated(This);
        return S_OK;
    }
    return IInternetProtocolSink_ReportResult(This->sink, result, error, text);
}

static IInternetProtocolSinkVtbl psink_vtbl = {
    psink_QueryInterface, psink_AddRef, psink_Release, psink_Switch,
    psink_ReportProgress, psink_ReportData, psink_ReportResult,
};

/*
 * IServiceProvider e IHttpNegotiate entregues ao tratador de rede quando há
 * entrada vencida: acrescentam If-Modified-Since e detectam o 304. O resto
 * vai para os do urlmon.
 */

static struct protocol *impl_from_service(IServiceProvider *iface)
{
    return CONTAINING_RECORD(iface, struct protocol, IServiceProvider_iface);
}

static HRESULT WINAPI service_QueryInterface(IServiceProvider *iface, REFIID riid, void **out)
{
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IServiceProvider)) {
        *out = iface;
        IServiceProvider_AddRef(iface);
        return S_OK;
    }
    return psink_QueryInterface(&impl_from_service(iface)->IInternetProtocolSink_iface, riid, out);
}

static ULONG WINAPI service_AddRef(IServiceProvider *iface)
{
    return protocol_AddRef(&impl_from_service(iface)->IInternetProtocol_iface);
}

static ULONG WINAPI service_Release(IServiceProvider *iface)
{
    return protocol_Release(&impl_from_service(iface)->IInternetProtocol_iface);
}

static HRESULT WINAPI service_QueryService(IServiceProvider *iface, REFGUID service, REFIID riid, void **out)
{
    struct protocol *This = impl_from_service(iface);
    IServiceProvider *outer;
    HRESULT hr;

    *out = NULL;
    hr = IInternetProtocolSink_QueryInterface(This->sink, &IID_IServiceProvider, (void **)&outer);
    if (FAILED(hr)) return hr;
    if (IsEqualGUID(service, &IID_IHttpNegotiate) && IsEqualIID(riid, &IID_IHttpNegotiate)) {
        if (!This->negotiate)
            hr = IServiceProvider_QueryService(outer, service, riid, (void **)&This->negotiate);
        if (SUCCEEDED(hr)) {
            *out = &This->IHttpNegotiate_iface;
            IHttpNegotiate_AddRef(&This->IHttpNegotiate_iface);
        }
    } else {
        hr = IServiceProvider_QueryService(outer, service, riid, out);
    }
    IServiceProvider_Release(outer);
    return hr;
}

static IServiceProviderVtbl service_vtbl = {
    service_QueryInterface, service_AddRef, service_Release, service_QueryService,
};

static struct protocol *impl_from_negotiate(IHttpNegotiate *iface)
{
    return CONTAINING_RECORD(iface, struct protocol, IHttpNegotiate_iface);
}

static HRESULT WINAPI negotiate_QueryInterface(IHttpNegotiate *iface, REFIID riid, void **out)
{
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IHttpNegotiate)) {
        *out = iface;
        IHttpNegotiate_AddRef(iface);
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI negotiate_AddRef(IHttpNegotiate *iface)
{
    return protocol_AddRef(&impl_from_negotiate(iface)->IInternetProtocol_iface);
}

static ULONG WINAPI negotiate_Release(IHttpNegotiate *iface)
{
    return protocol_Release(&impl_from_negotiate(iface)->IInternetProtocol_iface);
}

static HRESULT WINAPI negotiate_BeginningTransaction(IHttpNegotiate *iface, LPCWSTR url, LPCWSTR headers,
                                                     DWORD reserved, LPWSTR *additional)
{
    struct protocol *This = impl_from_negotiate(iface);
    ULONGLONG ticks = (ULONGLONG)This->stale.last_modified * 10000000ULL + 116444736000000000ULL;
    FILETIME ft = {(DWORD)ticks, (DWORD)(ticks >> 32)};
    char date[INTERNET_RFC1123_BUFSIZE], header[INTERNET_RFC1123_BUFSIZE + 32];
    SYSTEMTIME st;
    int old_len, len;
    LPWSTR combined;
    HRESULT hr;

    *additional = NULL;
    hr = IHttpNegotiate_BeginningTransaction(This->negotiate, url, headers, reserved, additional);
    if (FAILED(hr) || !FileTimeToSystemTime(&ft, &st) ||
        !InternetTimeFromSystemTimeA(&st, INTERNET_RFC1123_FORMAT, date, sizeof(date)))
        return hr;
    snprintf(header, sizeof(header), "If-Modified-Since: %s\r\n", date);
    len = MultiByteToWideChar(CP_ACP, 0, header, -1, NULL, 0); /* inclui o terminador */
    old_len = *additional ? lstrlenW(*additional) : 0;
    if (!(combined = CoTaskMemAlloc((old_len + len) * sizeof(WCHAR)))) return hr;
    if (old_len) memcpy(combined, *additional, old_len * sizeof(WCHAR));
    MultiByteToWideChar(CP_ACP, 0, header, -1, combined + old_len, len);
    CoTaskMemFree(*additional);
    *additional = combined;
    return S_OK;
}

static HRESULT WINAPI negotiate_OnResponse(IHttpNegotiate *iface, DWORD code, LPCWSTR response_headers,
                                           LPCWSTR request_headers, LPWSTR *additional)
{
    struct protocol *This = impl_from_negotiate(iface);
    fprintf(stderr, "flashhost: cache resposta %u %s\n", (unsigned)code, This->key);
    if (code == HTTP_STATUS_NOT_MODIFIED) {
        /*
         * Para o Flash, a resposta é o corpo do disco, como num acerto (em que
         * OnResponse nunca é chamado): os cabeçalhos do 304 (Content-Length: 0)
         * o fariam descartar o arquivo.
         */
        This->revalidated = TRUE;
        if (additional) *additional = NULL;
        return S_OK;
    }
    return IHttpNegotiate_OnResponse(This->negotiate, code, response_headers, request_headers, additional);
}

static IHttpNegotiateVtbl negotiate_vtbl = {
    negotiate_QueryInterface, negotiate_AddRef, negotiate_Release, negotiate_BeginningTransaction,
    negotiate_OnResponse,
};

/* ---- Fábrica e registro ---- */

static HRESULT WINAPI factory_QueryInterface(IClassFactory *iface, REFIID riid, void **out)
{
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IClassFactory)) {
        *out = iface;
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI factory_AddRef(IClassFactory *iface) { return 2; }
static ULONG WINAPI factory_Release(IClassFactory *iface) { return 1; }

static HRESULT WINAPI factory_CreateInstance(IClassFactory *iface, IUnknown *outer, REFIID riid, void **out)
{
    struct protocol *This;
    HRESULT hr;
    *out = NULL;
    if (outer) return CLASS_E_NOAGGREGATION;
    if (!(This = calloc(1, sizeof(*This)))) return E_OUTOFMEMORY;
    This->IInternetProtocol_iface.lpVtbl = &protocol_vtbl;
    This->IInternetProtocolSink_iface.lpVtbl = &psink_vtbl;
    This->IServiceProvider_iface.lpVtbl = &service_vtbl;
    This->IHttpNegotiate_iface.lpVtbl = &negotiate_vtbl;
    This->ref = 1;
    hr = IInternetProtocol_QueryInterface(&This->IInternetProtocol_iface, riid, out);
    IInternetProtocol_Release(&This->IInternetProtocol_iface);
    return hr;
}

static HRESULT WINAPI factory_LockServer(IClassFactory *iface, BOOL lock) { return S_OK; }

static IClassFactoryVtbl factory_vtbl = {
    factory_QueryInterface, factory_AddRef, factory_Release, factory_CreateInstance, factory_LockServer,
};
static IClassFactory factory = {&factory_vtbl};

void http_cache_install(const char *dir)
{
    IInternetSession *session;
    if (!dir || !*dir) return;
    snprintf(cache_dir, sizeof(cache_dir), "%s", dir);
    if (FAILED(CoInternetGetSession(0, &session, 0))) {
        fprintf(stderr, "flashhost: sessão do urlmon indisponível; cache desativado\n");
        return;
    }
    if (FAILED(IInternetSession_RegisterNameSpace(session, &factory, &CLSID_LegendHttpCache, L"http", 0, NULL, 0)) ||
        FAILED(IInternetSession_RegisterNameSpace(session, &factory, &CLSID_LegendHttpCache, L"https", 0, NULL, 0)))
        fprintf(stderr, "flashhost: não foi possível registrar o cache http\n");
    else
        fprintf(stderr, "flashhost: cache http em %s\n", cache_dir);
    IInternetSession_Release(session);
}
