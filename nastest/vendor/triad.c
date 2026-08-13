/* triad.c — 内存带宽基准(自研 STREAM 等价实现,作为官方 stream.c 下载失败时的兜底)
 *
 * 测量 Copy / Scale / Add / Triad 四种访存模式的带宽(MB/s)。
 * 编译:gcc -O3 -fopenmp -DARRAY_N=80000000 -o triad triad.c -lm
 * 运行:OMP_NUM_THREADS=$(nproc) ./triad
 *
 * 注:官方 STREAM 需从 https://www.cs.virginia.edu/stream/ 获取;
 * 本实现与其方法一致(大数组多次迭代取最优),但为独立代码,横向对比时请注明。
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#ifdef _OPENMP
#include <omp.h>
#endif

#ifndef ARRAY_N
#define ARRAY_N 80000000LL   /* 每数组元素数;80M double ≈ 610MB */
#endif
#ifndef NTIMES
#define NTIMES 10
#endif
#define SCALAR 3.0

static double *A, *B, *C;
static double best[4] = {0};  /* copy, scale, add, triad */
static const char *names[4] = {"Copy", "Scale", "Add", "Triad"};

static double now_s(void) {
#ifdef _OPENMP
    return omp_get_wtime();
#else
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
#endif
}

static void upd(int which, double secs) {
    double bytes;
    switch (which) {
        case 0: bytes = 2.0 * sizeof(double) * ARRAY_N; break; /* copy: r+w */
        case 1: bytes = 2.0 * sizeof(double) * ARRAY_N; break; /* scale */
        case 2: bytes = 3.0 * sizeof(double) * ARRAY_N; break; /* add */
        default: bytes = 3.0 * sizeof(double) * ARRAY_N; break; /* triad */
    }
    double mbs = bytes / secs / 1e6;
    if (mbs > best[which]) best[which] = mbs;
}

int main(void) {
    long long i;
    int t;
    A = malloc(sizeof(double) * ARRAY_N);
    B = malloc(sizeof(double) * ARRAY_N);
    C = malloc(sizeof(double) * ARRAY_N);
    if (!A || !B || !C) { fprintf(stderr, "malloc failed\n"); return 1; }

#ifdef _OPENMP
#pragma omp parallel for
#endif
    for (i = 0; i < ARRAY_N; i++) { A[i] = 1.0; B[i] = 2.0; C[i] = 0.0; }

#ifdef _OPENMP
    printf("threads=%d array=%.0f MB/each iters=%d\n",
           omp_get_max_threads(), sizeof(double) * ARRAY_N / 1e6, NTIMES);
#else
    printf("threads=1 array=%.0f MB/each iters=%d\n",
           sizeof(double) * ARRAY_N / 1e6, NTIMES);
#endif

    for (t = 0; t < NTIMES; t++) {
        double t0 = now_s();
#ifdef _OPENMP
#pragma omp parallel for
#endif
        for (i = 0; i < ARRAY_N; i++) C[i] = A[i];
        upd(0, now_s() - t0);

        t0 = now_s();
#ifdef _OPENMP
#pragma omp parallel for
#endif
        for (i = 0; i < ARRAY_N; i++) B[i] = SCALAR * C[i];
        upd(1, now_s() - t0);

        t0 = now_s();
#ifdef _OPENMP
#pragma omp parallel for
#endif
        for (i = 0; i < ARRAY_N; i++) C[i] = A[i] + B[i];
        upd(2, now_s() - t0);

        t0 = now_s();
#ifdef _OPENMP
#pragma omp parallel for
#endif
        for (i = 0; i < ARRAY_N; i++) A[i] = B[i] + SCALAR * C[i];
        upd(3, now_s() - t0);
    }

    for (t = 0; t < 4; t++)
        printf("%-8s %10.1f MB/s\n", names[t], best[t]);
    free(A); free(B); free(C);
    return 0;
}
