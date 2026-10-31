// Times the steps of sqlCipherExport (src/Simplex/Chat/Archive.hs) on a generated database.
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/sha.h>
#include <pthread.h>
#include "sqlite3.h"
#ifdef _WIN32
#include <windows.h>
#endif

#define KDF_ITER 256000
#define ROWS 20000
#define COMMITS 200

static double now(void) {
  struct timespec ts;
  timespec_get(&ts, TIME_UTC);
  return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void check(int rc, sqlite3 *db, const char *what) {
  if (rc != SQLITE_OK) {
    fprintf(stderr, "%s: %s\n", what, db ? sqlite3_errmsg(db) : sqlite3_errstr(rc));
    exit(1);
  }
}

static void exec_file(const char *path, const char *sql, const char *what) {
  sqlite3 *db;
  check(sqlite3_open(path, &db), db, what);
  check(sqlite3_exec(db, sql, NULL, NULL, NULL), db, what);
  sqlite3_close(db);
}

static void copy_file(const char *from, const char *to) {
#ifdef _WIN32
  if (!CopyFileA(from, to, FALSE)) { fprintf(stderr, "copy failed\n"); exit(1); }
#else
  FILE *in = fopen(from, "rb"), *out = fopen(to, "wb");
  char buf[65536];
  size_t n;
  while ((n = fread(buf, 1, sizeof buf, in)) > 0) fwrite(buf, 1, n, out);
  fclose(in);
  fclose(out);
#endif
}

static void replace_file(const char *from, const char *to) {
#ifdef _WIN32
  if (!MoveFileExA(from, to, MOVEFILE_REPLACE_EXISTING)) { fprintf(stderr, "rename failed\n"); exit(1); }
#else
  if (rename(from, to) != 0) { perror("rename"); exit(1); }
#endif
}

// The hashing that sntrup761 does through cbits/sha512.c of simplexmq
static volatile long hash_calls = 0;

static void *hash_loop(void *arg) {
  unsigned char in[1024] = {0}, out[64];
  (void)arg;
  for (;;) {
    SHA512(in, sizeof in, out);
    hash_calls++;
  }
  return NULL;
}

// The same CPU load without OpenSSL
static void *spin_loop(void *arg) {
  volatile unsigned long x = 0;
  (void)arg;
  for (;;) x++;
  return NULL;
}

#define STEP(name, code) do { double t0 = now(); code; printf("%-28s %8.3f s\n", name, now() - t0); } while (0)

int main(int argc, char **argv) {
  const char *dir = argc > 1 ? argv[1] : ".";
  int hashers = argc > 2 ? atoi(argv[2]) : 0;
  int spin = argc > 3 && strcmp(argv[3], "spin") == 0;
  pthread_t threads[64];
  for (int i = 0; i < hashers && i < 64; i++) pthread_create(&threads[i], NULL, spin ? spin_loop : hash_loop, NULL);
  char db[1024], bak[1024], exported[1024], sql[2048];
  snprintf(db, sizeof db, "%s/bench.db", dir);
  snprintf(bak, sizeof bak, "%s/bench.db.bak", dir);
  snprintf(exported, sizeof exported, "%s/bench.db.exported", dir);
  remove(db);
  remove(exported);
  printf("%s, SQLite %s, %d threads %s\n", OpenSSL_version(OPENSSL_VERSION), sqlite3_libversion(), hashers, spin ? "spinning" : "calling SHA512()");

  unsigned char out[64], salt[16] = {0};
  STEP("PBKDF2-HMAC-SHA512 256000", PKCS5_PBKDF2_HMAC("mykey", 5, salt, sizeof salt, KDF_ITER, EVP_sha512(), sizeof out, out));

  snprintf(sql, sizeof sql,
           "PRAGMA secure_delete = ON; PRAGMA auto_vacuum = FULL;"
           "CREATE TABLE t(id INTEGER PRIMARY KEY, a TEXT, b BLOB);"
           "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < %d)"
           " INSERT INTO t(a, b) SELECT printf('row %%d', i), randomblob(100) FROM n;",
           ROWS);
  STEP("create plain database", exec_file(db, sql, "create"));
  STEP("copy (backup)", copy_file(db, bak));
  snprintf(sql, sizeof sql,
           "ATTACH DATABASE '%s' AS exported KEY 'mykey'; SELECT sqlcipher_export('exported'); DETACH DATABASE exported;",
           exported);
  STEP("attach with key and export", exec_file(db, sql, "export"));
  STEP("test exported key", exec_file(exported, "PRAGMA key = 'mykey'; PRAGMA foreign_keys = ON; PRAGMA secure_delete = ON; SELECT count(*) FROM sqlite_master;", "test"));
  STEP("rename exported", replace_file(exported, db));
  STEP("open encrypted and read", exec_file(db, "PRAGMA key = 'mykey'; SELECT count(*) FROM t;", "read"));
  STEP("200 synced transactions", {
    sqlite3 *c;
    check(sqlite3_open(bak, &c), c, "commits");
    for (int i = 0; i < COMMITS; i++) check(sqlite3_exec(c, "INSERT INTO t(a) VALUES ('x');", NULL, NULL, NULL), c, "commit");
    sqlite3_close(c);
  });
  printf("SHA512() calls in other threads: %ld\n", hash_calls);
  fflush(stdout);
  _Exit(0); // OpenSSL cleanup at exit would race the hashing threads
}
