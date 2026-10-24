#ifndef FLASHHOST_HTTP_CACHE_H
#define FLASHHOST_HTTP_CACHE_H

/* Registra o cache de disco para http/https neste processo (diretório Unix). */
void http_cache_install(const char *dir);

#endif
