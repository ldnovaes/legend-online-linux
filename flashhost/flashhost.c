/*
 * flashhost: janela Win32 mínima que hospeda o controle ActiveX do Flash.
 *
 * Executado pelo Wine. Lê a configuração da entrada padrão, uma linha
 * "chave=valor" por vez, até EOF:
 *
 *   ocx=Z:\caminho\Flash64.ocx    controle a carregar (obrigatório)
 *   movie=https://.../Loading.swf  SWF inicial (obrigatório)
 *   flashvars=a=1&b=2              FlashVars da página
 *   prop.WMode=window              propriedade ActiveX qualquer
 *   title=Legend Online            título da janela
 *   width=1280 / height=760        área cliente inicial
 *   cache=/caminho/unix            cache de disco dos downloads (http_cache.c)
 *
 * O OCX é carregado diretamente (LoadLibrary + DllGetClassObject) e anexado
 * à janela com AtlAxAttachControl. A única entrada no registro do prefixo é a
 * typelib embutida no OCX, que o IDispatch do Flash exige (ver create_flash).
 */
#define COBJMACROS
#include <windows.h>
#include <ole2.h>
#include <ocidl.h>
#include <urlmon.h>
#include <atlbase.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "http_cache.h"

enum { EXIT_CONFIG = 2, EXIT_OCX = 3, EXIT_CONTROL = 4, EXIT_ATTACH = 5 };

static const CLSID CLSID_ShockwaveFlash =
    {0xd27cdb6e, 0xae6d, 0x11cf, {0x96, 0xb8, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00}};

#define MAX_PROPS 32

struct config {
    WCHAR *ocx, *movie, *flashvars, *title;
    char *cache;
    WCHAR *prop_names[MAX_PROPS], *prop_values[MAX_PROPS];
    char *prop_keys[MAX_PROPS];
    int props, width, height;
};

static HWND host_window;

/* Eventos do controle (DShockwaveFlashEvents). */
static const IID DIID_DShockwaveFlashEvents =
    {0xd27cdb6d, 0xae6d, 0x11cf, {0x96, 0xb8, 0x44, 0x45, 0x53, 0x54, 0x00, 0x00}};
enum { DISPID_FSCOMMAND = 150, DISPID_FLASHCALL = 197 };

static IDispatch *flash;

static void log_wide(const char *prefix, const WCHAR *text)
{
    int n = WideCharToMultiByte(CP_UTF8, 0, text, -1, NULL, 0, NULL, NULL);
    char *out = malloc(n);
    WideCharToMultiByte(CP_UTF8, 0, text, -1, out, n, NULL, NULL);
    fprintf(stderr, "flashhost: %s %s\n", prefix, out);
    free(out);
}

static HRESULT WINAPI sink_QueryInterface(IDispatch *iface, REFIID riid, void **out)
{
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IDispatch) ||
        IsEqualIID(riid, &DIID_DShockwaveFlashEvents)) {
        *out = iface;
        return S_OK;
    }
    *out = NULL;
    return E_NOINTERFACE;
}

static ULONG WINAPI sink_AddRef(IDispatch *iface) { return 2; }
static ULONG WINAPI sink_Release(IDispatch *iface) { return 1; }
static HRESULT WINAPI sink_GetTypeInfoCount(IDispatch *iface, UINT *count) { *count = 0; return S_OK; }
static HRESULT WINAPI sink_GetTypeInfo(IDispatch *iface, UINT i, LCID lcid, ITypeInfo **info) { return E_NOTIMPL; }
static HRESULT WINAPI sink_GetIDsOfNames(IDispatch *iface, REFIID riid, LPOLESTR *names, UINT n,
                                         LCID lcid, DISPID *ids) { return E_NOTIMPL; }

static HRESULT WINAPI sink_Invoke(IDispatch *iface, DISPID id, REFIID riid, LCID lcid, WORD flags,
                                  DISPPARAMS *params, VARIANT *result, EXCEPINFO *excep, UINT *arg)
{
    UINT i;
    if (id == DISPID_FLASHCALL && params->cArgs == 1 && V_VT(&params->rgvarg[0]) == VT_BSTR) {
        log_wide("FlashCall", V_BSTR(&params->rgvarg[0]));
    } else if (id == DISPID_FSCOMMAND) {
        /* Argumentos chegam em ordem inversa. */
        for (i = params->cArgs; i-- > 0;)
            if (V_VT(&params->rgvarg[i]) == VT_BSTR) log_wide("FSCommand", V_BSTR(&params->rgvarg[i]));
    } else {
        fprintf(stderr, "flashhost: evento %ld\n", (long)id);
    }
    return S_OK;
}

static IDispatchVtbl sink_vtbl = {
    sink_QueryInterface, sink_AddRef, sink_Release, sink_GetTypeInfoCount,
    sink_GetTypeInfo, sink_GetIDsOfNames, sink_Invoke,
};
static IDispatch event_sink = {&sink_vtbl};

/* Diagnóstico: registra o estado do controle alguns segundos após o carregamento. */
static void log_property(const WCHAR *name, WORD flags, VARIANT *arg)
{
    DISPID id;
    LPOLESTR names[1] = {(LPOLESTR)name};
    DISPPARAMS params = {arg, NULL, arg ? 1 : 0, 0};
    VARIANT result;
    char label[64];
    VariantInit(&result);
    WideCharToMultiByte(CP_UTF8, 0, name, -1, label, sizeof(label), NULL, NULL);
    if (FAILED(IDispatch_GetIDsOfNames(flash, &IID_NULL, names, 1, LOCALE_USER_DEFAULT, &id)) ||
        FAILED(IDispatch_Invoke(flash, id, &IID_NULL, LOCALE_USER_DEFAULT, flags, &params,
                                &result, NULL, NULL)) ||
        FAILED(VariantChangeType(&result, &result, 0, VT_BSTR))) {
        fprintf(stderr, "flashhost: estado %s indisponível\n", label);
        return;
    }
    log_wide(label, V_BSTR(&result));
    VariantClear(&result);
}

static void CALLBACK report_state(HWND hwnd, UINT msg, UINT_PTR timer, DWORD time)
{
    VARIANT name;
    KillTimer(hwnd, timer);
    log_property(L"ReadyState", DISPATCH_PROPERTYGET, NULL);
    log_property(L"PercentLoaded", DISPATCH_METHOD, NULL);
    log_property(L"TotalFrames", DISPATCH_PROPERTYGET, NULL);
    log_property(L"AllowScriptAccess", DISPATCH_PROPERTYGET, NULL);
    V_VT(&name) = VT_BSTR;
    V_BSTR(&name) = SysAllocString(L"$version");
    log_property(L"GetVariable", DISPATCH_METHOD, &name);
    VariantClear(&name);
}

static void listen_events(IUnknown *control)
{
    IConnectionPointContainer *container;
    IConnectionPoint *point;
    DWORD cookie;
    if (FAILED(IUnknown_QueryInterface(control, &IID_IConnectionPointContainer, (void **)&container)))
        return;
    if (SUCCEEDED(IConnectionPointContainer_FindConnectionPoint(container, &DIID_DShockwaveFlashEvents, &point))) {
        if (FAILED(IConnectionPoint_Advise(point, (IUnknown *)&event_sink, &cookie)))
            fprintf(stderr, "flashhost: não foi possível receber eventos do Flash\n");
        IConnectionPoint_Release(point);
    }
    IConnectionPointContainer_Release(container);
}

static WCHAR *widen(const char *text)
{
    int n = MultiByteToWideChar(CP_UTF8, 0, text, -1, NULL, 0);
    WCHAR *out = malloc(n * sizeof(WCHAR));
    MultiByteToWideChar(CP_UTF8, 0, text, -1, out, n);
    return out;
}

static char *read_all(FILE *in)
{
    size_t size = 0, cap = 4096, got;
    char *buf = malloc(cap);
    while ((got = fread(buf + size, 1, cap - size - 1, in)) > 0) {
        size += got;
        if (cap - size < 2) buf = realloc(buf, cap *= 2);
    }
    buf[size] = 0;
    return buf;
}

static int parse_config(char *text, struct config *cfg)
{
    char *line, *save = NULL;
    cfg->width = 1280;
    cfg->height = 760;
    for (line = strtok_r(text, "\r\n", &save); line; line = strtok_r(NULL, "\r\n", &save)) {
        char *value = strchr(line, '=');
        if (!value) continue;
        *value++ = 0;
        if (!strcmp(line, "ocx")) cfg->ocx = widen(value);
        else if (!strcmp(line, "movie")) cfg->movie = widen(value);
        else if (!strcmp(line, "flashvars")) cfg->flashvars = widen(value);
        else if (!strcmp(line, "title")) cfg->title = widen(value);
        else if (!strcmp(line, "cache")) cfg->cache = value;
        else if (!strcmp(line, "width")) cfg->width = atoi(value);
        else if (!strcmp(line, "height")) cfg->height = atoi(value);
        else if (!strncmp(line, "prop.", 5) && cfg->props < MAX_PROPS) {
            cfg->prop_keys[cfg->props] = line + 5;
            cfg->prop_names[cfg->props] = widen(line + 5);
            cfg->prop_values[cfg->props++] = widen(value);
        }
    }
    return cfg->ocx && cfg->movie;
}

static HRESULT put_property(IDispatch *control, const WCHAR *name, const WCHAR *value)
{
    DISPID id, named = DISPID_PROPERTYPUT;
    LPOLESTR names[1] = {(LPOLESTR)name};
    VARIANT arg;
    DISPPARAMS params = {&arg, &named, 1, 1};
    HRESULT hr = IDispatch_GetIDsOfNames(control, &IID_NULL, names, 1, LOCALE_USER_DEFAULT, &id);
    if (FAILED(hr)) return hr;
    VariantInit(&arg);
    arg.vt = VT_BSTR;
    arg.bstrVal = SysAllocString(value);
    hr = IDispatch_Invoke(control, id, &IID_NULL, LOCALE_USER_DEFAULT, DISPATCH_PROPERTYPUT,
                          &params, NULL, NULL, NULL);
    VariantClear(&arg);
    return hr;
}

/*
 * Correção de download: o Flash devolve E_PENDING em OnDataAvailable quando já
 * leu tudo o que havia disponível. O Windows ignora esse retorno; o urlmon do
 * Wine o trata como erro e encerra o download no primeiro bloco. Interceptamos
 * RegisterBindStatusCallback na tabela de importação do OCX e embrulhamos o
 * callback do Flash para que E_PENDING chegue ao Wine como S_OK.
 */
struct bsc_wrapper {
    IBindStatusCallback IBindStatusCallback_iface;
    LONG ref;
    IBindStatusCallback *inner;
};

static struct bsc_wrapper *impl_from_bsc(IBindStatusCallback *iface)
{
    return CONTAINING_RECORD(iface, struct bsc_wrapper, IBindStatusCallback_iface);
}

static HRESULT WINAPI bsc_QueryInterface(IBindStatusCallback *iface, REFIID riid, void **out)
{
    struct bsc_wrapper *This = impl_from_bsc(iface);
    if (IsEqualIID(riid, &IID_IUnknown) || IsEqualIID(riid, &IID_IBindStatusCallback)) {
        *out = iface;
        IBindStatusCallback_AddRef(iface);
        return S_OK;
    }
    return IBindStatusCallback_QueryInterface(This->inner, riid, out);
}

static ULONG WINAPI bsc_AddRef(IBindStatusCallback *iface)
{
    return InterlockedIncrement(&impl_from_bsc(iface)->ref);
}

static ULONG WINAPI bsc_Release(IBindStatusCallback *iface)
{
    struct bsc_wrapper *This = impl_from_bsc(iface);
    ULONG ref = InterlockedDecrement(&This->ref);
    if (!ref) {
        IBindStatusCallback_Release(This->inner);
        free(This);
    }
    return ref;
}

static HRESULT WINAPI bsc_OnStartBinding(IBindStatusCallback *iface, DWORD reserved, IBinding *binding)
{
    return IBindStatusCallback_OnStartBinding(impl_from_bsc(iface)->inner, reserved, binding);
}

static HRESULT WINAPI bsc_GetPriority(IBindStatusCallback *iface, LONG *priority)
{
    return IBindStatusCallback_GetPriority(impl_from_bsc(iface)->inner, priority);
}

static HRESULT WINAPI bsc_OnLowResource(IBindStatusCallback *iface, DWORD reserved)
{
    return IBindStatusCallback_OnLowResource(impl_from_bsc(iface)->inner, reserved);
}

static HRESULT WINAPI bsc_OnProgress(IBindStatusCallback *iface, ULONG progress, ULONG max,
                                     ULONG code, LPCWSTR text)
{
    return IBindStatusCallback_OnProgress(impl_from_bsc(iface)->inner, progress, max, code, text);
}

static HRESULT WINAPI bsc_OnStopBinding(IBindStatusCallback *iface, HRESULT result, LPCWSTR error)
{
    return IBindStatusCallback_OnStopBinding(impl_from_bsc(iface)->inner, result, error);
}

static HRESULT WINAPI bsc_GetBindInfo(IBindStatusCallback *iface, DWORD *flags, BINDINFO *info)
{
    return IBindStatusCallback_GetBindInfo(impl_from_bsc(iface)->inner, flags, info);
}

static HRESULT WINAPI bsc_OnDataAvailable(IBindStatusCallback *iface, DWORD flags, DWORD size,
                                          FORMATETC *format, STGMEDIUM *medium)
{
    HRESULT hr = IBindStatusCallback_OnDataAvailable(impl_from_bsc(iface)->inner, flags, size,
                                                     format, medium);
    return hr == E_PENDING ? S_OK : hr;
}

static HRESULT WINAPI bsc_OnObjectAvailable(IBindStatusCallback *iface, REFIID riid, IUnknown *obj)
{
    return IBindStatusCallback_OnObjectAvailable(impl_from_bsc(iface)->inner, riid, obj);
}

static IBindStatusCallbackVtbl bsc_vtbl = {
    bsc_QueryInterface, bsc_AddRef, bsc_Release, bsc_OnStartBinding, bsc_GetPriority,
    bsc_OnLowResource, bsc_OnProgress, bsc_OnStopBinding, bsc_GetBindInfo,
    bsc_OnDataAvailable, bsc_OnObjectAvailable,
};

static HRESULT (WINAPI *real_RegisterBindStatusCallback)(IBindCtx *, IBindStatusCallback *,
                                                         IBindStatusCallback **, DWORD);

static HRESULT WINAPI hook_RegisterBindStatusCallback(IBindCtx *ctx, IBindStatusCallback *callback,
                                                      IBindStatusCallback **previous, DWORD reserved)
{
    struct bsc_wrapper *wrapper;
    HRESULT hr;
    if (!callback || !(wrapper = malloc(sizeof(*wrapper))))
        return real_RegisterBindStatusCallback(ctx, callback, previous, reserved);
    wrapper->IBindStatusCallback_iface.lpVtbl = &bsc_vtbl;
    wrapper->ref = 1;
    wrapper->inner = callback;
    IBindStatusCallback_AddRef(callback);
    hr = real_RegisterBindStatusCallback(ctx, &wrapper->IBindStatusCallback_iface, previous, reserved);
    IBindStatusCallback_Release(&wrapper->IBindStatusCallback_iface);
    return hr;
}

static BOOL patch_import(HMODULE module, const char *dll, const char *name, void *hook, void **original)
{
    BYTE *base = (BYTE *)module;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + ((IMAGE_DOS_HEADER *)base)->e_lfanew);
    IMAGE_DATA_DIRECTORY *dir = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    IMAGE_IMPORT_DESCRIPTOR *desc = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir->VirtualAddress);
    for (; dir->VirtualAddress && desc->Name; desc++) {
        IMAGE_THUNK_DATA *names, *slots;
        if (lstrcmpiA((char *)(base + desc->Name), dll) || !desc->OriginalFirstThunk) continue;
        names = (IMAGE_THUNK_DATA *)(base + desc->OriginalFirstThunk);
        slots = (IMAGE_THUNK_DATA *)(base + desc->FirstThunk);
        for (; names->u1.AddressOfData; names++, slots++) {
            DWORD protect;
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal) ||
                strcmp((char *)((IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData))->Name, name))
                continue;
            VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), PAGE_READWRITE, &protect);
            *original = (void *)slots->u1.Function;
            slots->u1.Function = (ULONG_PTR)hook;
            VirtualProtect(&slots->u1.Function, sizeof(slots->u1.Function), protect, &protect);
            return TRUE;
        }
    }
    return FALSE;
}

static IUnknown *create_flash(const WCHAR *ocx_path)
{
    HRESULT (WINAPI *get_class)(REFCLSID, REFIID, void **);
    IClassFactory *factory;
    IUnknown *control = NULL;
    ITypeLib *typelib;
    HMODULE ocx = LoadLibraryW(ocx_path);
    if (!ocx) {
        fprintf(stderr, "flashhost: LoadLibrary do OCX falhou (erro %u)\n", (unsigned)GetLastError());
        return NULL;
    }
    /* O IDispatch do Flash resolve nomes (Movie, FlashVars...) pela typelib
     * registrada; sem ela todas as propriedades são recusadas. Registrar a
     * cada execução mantém o caminho correto se o OCX mudar de lugar. */
    if (SUCCEEDED(LoadTypeLibEx(ocx_path, REGKIND_REGISTER, &typelib)))
        ITypeLib_Release(typelib);
    else
        fprintf(stderr, "flashhost: aviso: typelib do OCX não registrada\n");
    if (!patch_import(ocx, "urlmon.dll", "RegisterBindStatusCallback",
                      hook_RegisterBindStatusCallback, (void **)&real_RegisterBindStatusCallback))
        fprintf(stderr, "flashhost: aviso: correção de download não aplicada\n");
    get_class = (void *)GetProcAddress(ocx, "DllGetClassObject");
    if (!get_class || FAILED(get_class(&CLSID_ShockwaveFlash, &IID_IClassFactory, (void **)&factory))) {
        fprintf(stderr, "flashhost: OCX não fornece a classe ShockwaveFlash\n");
        return NULL;
    }
    if (FAILED(IClassFactory_CreateInstance(factory, NULL, &IID_IUnknown, (void **)&control)))
        fprintf(stderr, "flashhost: falha ao instanciar o controle Flash\n");
    IClassFactory_Release(factory);
    return control;
}

static LRESULT CALLBACK frame_proc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam)
{
    switch (msg) {
    case WM_SIZE:
        if (host_window) MoveWindow(host_window, 0, 0, LOWORD(lparam), HIWORD(lparam), TRUE);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wparam, lparam);
}

static HWND create_frame(const struct config *cfg)
{
    WNDCLASSW cls = {0};
    RECT rect = {0, 0, cfg->width, cfg->height}, work;
    int width, height;
    cls.lpfnWndProc = frame_proc;
    cls.hInstance = GetModuleHandleW(NULL);
    cls.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_ARROW);
    cls.hbrBackground = GetStockObject(BLACK_BRUSH);
    cls.lpszClassName = L"LegendClientFlashFrame";
    RegisterClassW(&cls);
    AdjustWindowRect(&rect, WS_OVERLAPPEDWINDOW, FALSE);
    /* Centraliza na área útil do monitor principal. */
    SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
    width = min(rect.right - rect.left, work.right - work.left);
    height = min(rect.bottom - rect.top, work.bottom - work.top);
    return CreateWindowW(cls.lpszClassName, cfg->title ? cfg->title : L"Legend Online",
                         WS_OVERLAPPEDWINDOW | WS_CLIPCHILDREN,
                         work.left + (work.right - work.left - width) / 2,
                         work.top + (work.bottom - work.top - height) / 2,
                         width, height, NULL, NULL, cls.hInstance, NULL);
}

int main(void)
{
    struct config cfg = {0};
    IUnknown *control, *container = NULL;
    IDispatch *dispatch;
    HWND frame;
    RECT client;
    MSG msg;
    int i;

    if (!parse_config(read_all(stdin), &cfg)) {
        fprintf(stderr, "flashhost: configuração incompleta (ocx e movie são obrigatórios)\n");
        return EXIT_CONFIG;
    }
    OleInitialize(NULL);
    http_cache_install(cfg.cache);
    AtlAxWinInit();
    if (!(control = create_flash(cfg.ocx))) return EXIT_OCX;
    if (FAILED(IUnknown_QueryInterface(control, &IID_IDispatch, (void **)&dispatch))) {
        fprintf(stderr, "flashhost: controle sem IDispatch\n");
        return EXIT_CONTROL;
    }

    flash = dispatch;
    listen_events(control);
    frame = create_frame(&cfg);
    GetClientRect(frame, &client);
    host_window = CreateWindowW(L"STATIC", NULL, WS_CHILD | WS_VISIBLE, 0, 0, client.right,
                                client.bottom, frame, NULL, GetModuleHandleW(NULL), NULL);
    if (FAILED(AtlAxAttachControl(control, host_window, &container))) {
        fprintf(stderr, "flashhost: AtlAxAttachControl falhou\n");
        return EXIT_ATTACH;
    }

    for (i = 0; i < cfg.props; i++)
        if (FAILED(put_property(dispatch, cfg.prop_names[i], cfg.prop_values[i])))
            fprintf(stderr, "flashhost: propriedade %s recusada\n", cfg.prop_keys[i]);
    if (cfg.flashvars && FAILED(put_property(dispatch, L"FlashVars", cfg.flashvars)))
        fprintf(stderr, "flashhost: FlashVars recusado\n");
    if (FAILED(put_property(dispatch, L"Movie", cfg.movie))) {
        fprintf(stderr, "flashhost: Movie recusado\n");
        return EXIT_CONTROL;
    }
    fprintf(stderr, "flashhost: movie carregado\n");

    ShowWindow(frame, SW_SHOW);
    SetForegroundWindow(frame);
    SetTimer(frame, 1, 8000, report_state);
    while (GetMessageW(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    IDispatch_Release(dispatch);
    if (container) IUnknown_Release(container);
    IUnknown_Release(control);
    OleUninitialize();
    return 0;
}
