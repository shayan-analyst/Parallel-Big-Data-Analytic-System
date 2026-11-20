# cython: language_level=3, boundscheck=False, wraparound=False, initializedcheck=False, cdivision=True
"""Native sequential and OpenMP kernels with block-level work partitioning."""
import numpy as np
cimport numpy as cnp
from cython.parallel cimport prange, threadid

ctypedef cnp.float64_t float64_t
ctypedef cnp.int32_t int32_t

cdef Py_ssize_t BLOCK_ROWS = 131072
cdef Py_ssize_t CACHE_LINE = 64


def _aligned_zeros(shape, dtype):
    """Zeroed C-contiguous array whose first byte sits on a 64-byte cache line.

    Per-thread accumulator rows are padded to a multiple of 64 bytes by the
    callers. Together with this base alignment no two threads ever write to the
    same cache line (no false sharing), whatever the allocator returned. Without
    it, neighbouring threads' counters share a line and every increment forces
    that line to bounce between cores, which can make 4 threads slower than 1.
    """
    dt = np.dtype(dtype)
    count = 1
    for dim in shape:
        count *= int(dim)
    nbytes = count * dt.itemsize
    raw = np.zeros(nbytes + CACHE_LINE, dtype=np.uint8)
    address = raw.__array_interface__["data"][0]
    offset = (CACHE_LINE - (int(address) % CACHE_LINE)) % CACHE_LINE  # non-negative operands: safe under cdivision
    return raw[offset:offset + nbytes].view(dt).reshape(shape)


def engine_info():
    return {
        "compiled": True,
        "engine": "Cython + OpenMP",
        "openmp": True,
        "partitioning": "contiguous record blocks",
        "false_sharing_guard": True,
        "block_rows": BLOCK_ROWS,
        "scheduling": ["static", "dynamic", "guided"],
        "kernels": ["numeric_stats", "filter_gt", "frequency", "group_sum", "comprehensive_analytics"],
    }


def numeric_stats_seq(cnp.ndarray[float64_t, ndim=1, mode="c"] values):
    cdef Py_ssize_t i, n = values.shape[0]
    cdef double[::1] x = values
    cdef double total = 0.0
    cdef double sumsq = 0.0
    cdef double minimum = 0.0
    cdef double maximum = 0.0
    if n == 0:
        return (0, 0.0, 0.0, None, None)
    minimum = x[0]
    maximum = x[0]
    for i in range(n):
        total += x[i]
        sumsq += x[i] * x[i]
        if x[i] < minimum:
            minimum = x[i]
        if x[i] > maximum:
            maximum = x[i]
    return (n, total, sumsq, minimum, maximum)


def numeric_stats_parallel(cnp.ndarray[float64_t, ndim=1, mode="c"] values,
                            int threads=2, str schedule="static"):
    cdef Py_ssize_t i, n = values.shape[0]
    cdef Py_ssize_t chunk, start, end, n_chunks = (n + BLOCK_ROWS - 1) // BLOCK_ROWS
    cdef int t, tid
    cdef double[::1] x = values
    cdef cnp.ndarray[float64_t, ndim=2] partials = _aligned_zeros((threads, 8), np.float64)
    cdef cnp.ndarray[int32_t, ndim=1] seen_arr = np.zeros(threads, dtype=np.int32)
    cdef double[:, ::1] p = partials
    cdef int[::1] seen = seen_arr
    cdef double local_sum, local_sumsq, local_min, local_max
    cdef long long local_count
    for t in range(threads):
        p[t, 3] = 1.7976931348623157e308
        p[t, 4] = -1.7976931348623157e308
    if n == 0:
        return (0, 0.0, 0.0, None, None, 0)
    if schedule == "dynamic":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="dynamic", chunksize=1):
            tid = threadid()
            start = chunk * BLOCK_ROWS
            end = start + BLOCK_ROWS
            if end > n: end = n
            local_sum = 0.0
            local_sumsq = 0.0
            local_count = 0
            local_min = 1.7976931348623157e308
            local_max = -1.7976931348623157e308
            for i in range(start, end):
                local_sum = local_sum + x[i]
                local_sumsq = local_sumsq + x[i] * x[i]
                local_count = local_count + 1
                if x[i] < local_min: local_min = x[i]
                if x[i] > local_max: local_max = x[i]
            seen[tid] = 1
            p[tid, 0] += local_count
            p[tid, 1] += local_sum
            p[tid, 2] += local_sumsq
            if local_min < p[tid, 3]: p[tid, 3] = local_min
            if local_max > p[tid, 4]: p[tid, 4] = local_max
    elif schedule == "guided":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="guided", chunksize=1):
            tid = threadid()
            start = chunk * BLOCK_ROWS
            end = start + BLOCK_ROWS
            if end > n: end = n
            local_sum = 0.0
            local_sumsq = 0.0
            local_count = 0
            local_min = 1.7976931348623157e308
            local_max = -1.7976931348623157e308
            for i in range(start, end):
                local_sum = local_sum + x[i]
                local_sumsq = local_sumsq + x[i] * x[i]
                local_count = local_count + 1
                if x[i] < local_min: local_min = x[i]
                if x[i] > local_max: local_max = x[i]
            seen[tid] = 1
            p[tid, 0] += local_count
            p[tid, 1] += local_sum
            p[tid, 2] += local_sumsq
            if local_min < p[tid, 3]: p[tid, 3] = local_min
            if local_max > p[tid, 4]: p[tid, 4] = local_max
    else:
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="static"):
            tid = threadid()
            start = chunk * BLOCK_ROWS
            end = start + BLOCK_ROWS
            if end > n: end = n
            local_sum = 0.0
            local_sumsq = 0.0
            local_count = 0
            local_min = 1.7976931348623157e308
            local_max = -1.7976931348623157e308
            for i in range(start, end):
                local_sum = local_sum + x[i]
                local_sumsq = local_sumsq + x[i] * x[i]
                local_count = local_count + 1
                if x[i] < local_min: local_min = x[i]
                if x[i] > local_max: local_max = x[i]
            seen[tid] = 1
            p[tid, 0] += local_count
            p[tid, 1] += local_sum
            p[tid, 2] += local_sumsq
            if local_min < p[tid, 3]: p[tid, 3] = local_min
            if local_max > p[tid, 4]: p[tid, 4] = local_max
    cdef long long count = 0
    cdef double total = 0.0
    cdef double sumsq = 0.0
    cdef double minimum = 1.7976931348623157e308
    cdef double maximum = -1.7976931348623157e308
    cdef int actual = 0
    for t in range(threads):
        count += <long long>p[t, 0]
        total += p[t, 1]
        sumsq += p[t, 2]
        if p[t, 0] > 0:
            if p[t, 3] < minimum: minimum = p[t, 3]
            if p[t, 4] > maximum: maximum = p[t, 4]
        if seen[t]: actual += 1
    return (count, total, sumsq, minimum, maximum, actual)


def threshold_count_seq(cnp.ndarray[float64_t, ndim=1, mode="c"] values,
                        double threshold):
    cdef Py_ssize_t i, n = values.shape[0]
    cdef double[::1] x = values
    cdef long long count = 0
    for i in range(n):
        if x[i] > threshold:
            count += 1
    return count


def threshold_count_parallel(cnp.ndarray[float64_t, ndim=1, mode="c"] values,
                             double threshold, int threads=2,
                             str schedule="static"):
    cdef Py_ssize_t i, n = values.shape[0]
    cdef Py_ssize_t chunk, start, end, n_chunks = (n + BLOCK_ROWS - 1) // BLOCK_ROWS
    cdef int t, tid
    cdef double[::1] x = values
    cdef cnp.ndarray[cnp.int64_t, ndim=1] partials = np.zeros(threads, dtype=np.int64)
    cdef cnp.ndarray[int32_t, ndim=1] seen_arr = np.zeros(threads, dtype=np.int32)
    cdef long long[::1] p = partials
    cdef int[::1] seen = seen_arr
    cdef long long local_count
    if schedule == "dynamic":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="dynamic", chunksize=1):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n); local_count = 0
            for i in range(start, end):
                if x[i] > threshold: local_count = local_count + 1
            seen[tid] = 1; p[tid] += local_count
    elif schedule == "guided":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="guided", chunksize=1):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n); local_count = 0
            for i in range(start, end):
                if x[i] > threshold: local_count = local_count + 1
            seen[tid] = 1; p[tid] += local_count
    else:
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="static"):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n); local_count = 0
            for i in range(start, end):
                if x[i] > threshold: local_count = local_count + 1
            seen[tid] = 1; p[tid] += local_count
    cdef long long total = 0
    for t in range(threads): total += p[t]
    return (total, int(np.count_nonzero(seen_arr)))


def frequency_seq(cnp.ndarray[int32_t, ndim=1, mode="c"] codes, int category_count):
    cdef Py_ssize_t i, n = codes.shape[0]
    cdef int[::1] x = codes
    cdef cnp.ndarray[cnp.int64_t, ndim=1] out = np.zeros(category_count, dtype=np.int64)
    cdef long long[::1] result = out
    for i in range(n): result[x[i]] += 1
    return out


def frequency_parallel(cnp.ndarray[int32_t, ndim=1, mode="c"] codes,
                       int category_count, int threads=2,
                       str schedule="static"):
    cdef Py_ssize_t i, n = codes.shape[0]
    cdef Py_ssize_t chunk, start, end, n_chunks = (n + BLOCK_ROWS - 1) // BLOCK_ROWS
    cdef int t, tid
    cdef int[::1] x = codes
    cdef int padded_categories = ((category_count * 8 + 63) // 64) * 8
    cdef cnp.ndarray[cnp.int64_t, ndim=2] partials = _aligned_zeros((threads, padded_categories), np.int64)
    cdef cnp.ndarray[int32_t, ndim=1] seen_arr = np.zeros(threads, dtype=np.int32)
    cdef long long[:, ::1] p = partials
    cdef int[::1] seen = seen_arr
    if schedule == "dynamic":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="dynamic", chunksize=1):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n)
            for i in range(start, end): p[tid, x[i]] += 1
            seen[tid] = 1
    elif schedule == "guided":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="guided", chunksize=1):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n)
            for i in range(start, end): p[tid, x[i]] += 1
            seen[tid] = 1
    else:
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="static"):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n)
            for i in range(start, end): p[tid, x[i]] += 1
            seen[tid] = 1
    return np.asarray(np.sum(partials[:, :category_count], axis=0), dtype=np.int64), int(np.count_nonzero(seen_arr))


def group_sum_seq(cnp.ndarray[int32_t, ndim=1, mode="c"] codes,
                  cnp.ndarray[float64_t, ndim=1, mode="c"] values,
                  int category_count):
    cdef Py_ssize_t i, n = codes.shape[0]
    cdef int[::1] c = codes
    cdef double[::1] x = values
    cdef cnp.ndarray[cnp.int64_t, ndim=1] counts_arr = np.zeros(category_count, dtype=np.int64)
    cdef cnp.ndarray[float64_t, ndim=1] sums_arr = np.zeros(category_count, dtype=np.float64)
    cdef long long[::1] counts = counts_arr
    cdef double[::1] sums = sums_arr
    for i in range(n): counts[c[i]] += 1; sums[c[i]] += x[i]
    return counts_arr, sums_arr


def group_sum_parallel(cnp.ndarray[int32_t, ndim=1, mode="c"] codes,
                       cnp.ndarray[float64_t, ndim=1, mode="c"] values,
                       int category_count, int threads=2,
                       str schedule="static"):
    cdef Py_ssize_t i, n = codes.shape[0]
    cdef Py_ssize_t chunk, start, end, n_chunks = (n + BLOCK_ROWS - 1) // BLOCK_ROWS
    cdef int t, tid
    cdef int[::1] c = codes
    cdef double[::1] x = values
    cdef int padded_categories = ((category_count * 8 + 63) // 64) * 8
    cdef cnp.ndarray[cnp.int64_t, ndim=2] partial_counts = _aligned_zeros((threads, padded_categories), np.int64)
    cdef cnp.ndarray[float64_t, ndim=2] partial_sums = _aligned_zeros((threads, padded_categories), np.float64)
    cdef cnp.ndarray[int32_t, ndim=1] seen_arr = np.zeros(threads, dtype=np.int32)
    cdef long long[:, ::1] counts = partial_counts
    cdef double[:, ::1] sums = partial_sums
    cdef int[::1] seen = seen_arr
    if schedule == "dynamic":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="dynamic", chunksize=1):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n)
            for i in range(start, end): counts[tid, c[i]] += 1; sums[tid, c[i]] += x[i]
            seen[tid] = 1
    elif schedule == "guided":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="guided", chunksize=1):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n)
            for i in range(start, end): counts[tid, c[i]] += 1; sums[tid, c[i]] += x[i]
            seen[tid] = 1
    else:
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="static"):
            tid = threadid(); start = chunk * BLOCK_ROWS; end = min(start + BLOCK_ROWS, n)
            for i in range(start, end): counts[tid, c[i]] += 1; sums[tid, c[i]] += x[i]
            seen[tid] = 1
    return (np.sum(partial_counts[:, :category_count], axis=0).astype(np.int64),
            np.sum(partial_sums[:, :category_count], axis=0).astype(np.float64),
            int(np.count_nonzero(seen_arr)))



def comprehensive_analytics_seq(cnp.ndarray[int32_t, ndim=1, mode="c"] codes,
                                cnp.ndarray[float64_t, ndim=1, mode="c"] values,
                                int category_count, double threshold):
    """One-pass business analytics: numeric profile, threshold count and group metrics."""
    cdef Py_ssize_t i, n = codes.shape[0]
    cdef int[::1] c = codes
    cdef double[::1] x = values
    cdef cnp.ndarray[cnp.int64_t, ndim=1] counts_arr = np.zeros(category_count, dtype=np.int64)
    cdef cnp.ndarray[float64_t, ndim=1] sums_arr = np.zeros(category_count, dtype=np.float64)
    cdef long long[::1] group_counts = counts_arr
    cdef double[::1] group_sums = sums_arr
    cdef long long total_count = 0
    cdef long long above_count = 0
    cdef double total = 0.0
    cdef double sumsq = 0.0
    cdef double minimum = 1.7976931348623157e308
    cdef double maximum = -1.7976931348623157e308
    cdef double value
    with nogil:
        for i in range(n):
            value = x[i]
            total_count += 1
            total += value
            sumsq += value * value
            if value < minimum:
                minimum = value
            if value > maximum:
                maximum = value
            if value > threshold:
                above_count += 1
            group_counts[c[i]] += 1
            group_sums[c[i]] += value
    if n == 0:
        minimum = 0.0
        maximum = 0.0
    return (total_count, total, sumsq, minimum, maximum, above_count,
            counts_arr, sums_arr)


def comprehensive_analytics_parallel(cnp.ndarray[int32_t, ndim=1, mode="c"] codes,
                                     cnp.ndarray[float64_t, ndim=1, mode="c"] values,
                                     int category_count, double threshold,
                                     int threads=2, str schedule="static"):
    """OpenMP one-pass multi-metric aggregation with thread-local group accumulators."""
    cdef Py_ssize_t i, n = codes.shape[0]
    cdef Py_ssize_t chunk, start, end, n_chunks = (n + BLOCK_ROWS - 1) // BLOCK_ROWS
    cdef int tid, t
    cdef int[::1] c = codes
    cdef double[::1] x = values
    # Pad each worker's private accumulator row to a cache-line multiple.
    # Without padding, neighbouring workers can repeatedly invalidate the same
    # cache line (false sharing), especially for low-cardinality group columns.
    cdef int padded_categories = ((category_count * 8 + 63) // 64) * 8
    cdef cnp.ndarray[float64_t, ndim=2] stats_arr = _aligned_zeros((threads, 8), np.float64)
    cdef cnp.ndarray[cnp.int64_t, ndim=2] partial_counts = _aligned_zeros((threads, padded_categories), np.int64)
    cdef cnp.ndarray[float64_t, ndim=2] partial_sums = _aligned_zeros((threads, padded_categories), np.float64)
    cdef cnp.ndarray[int32_t, ndim=1] seen_arr = np.zeros(threads, dtype=np.int32)
    cdef double[:, ::1] stats = stats_arr
    cdef long long[:, ::1] group_counts = partial_counts
    cdef double[:, ::1] group_sums = partial_sums
    cdef int[::1] seen = seen_arr
    cdef long long local_count, local_above
    cdef double local_sum, local_sumsq, local_min, local_max, value
    cdef long long total_count = 0
    cdef long long above_count = 0
    cdef double total = 0.0
    cdef double sumsq = 0.0
    cdef double minimum = 1.7976931348623157e308
    cdef double maximum = -1.7976931348623157e308
    cdef int actual = 0

    stats_arr[:, 2] = 1.7976931348623157e308
    stats_arr[:, 3] = -1.7976931348623157e308

    if schedule == "dynamic":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="dynamic", chunksize=1):
            tid = threadid()
            start = chunk * BLOCK_ROWS
            end = min(start + BLOCK_ROWS, n)
            local_count = 0
            local_above = 0
            local_sum = 0.0
            local_sumsq = 0.0
            local_min = 1.7976931348623157e308
            local_max = -1.7976931348623157e308
            for i in range(start, end):
                value = x[i]
                local_count = local_count + 1
                local_sum = local_sum + value
                local_sumsq = local_sumsq + value * value
                if value < local_min:
                    local_min = value
                if value > local_max:
                    local_max = value
                if value > threshold:
                    local_above = local_above + 1
                group_counts[tid, c[i]] += 1
                group_sums[tid, c[i]] += value
            stats[tid, 0] += local_sum
            stats[tid, 1] += local_sumsq
            if local_min < stats[tid, 2]:
                stats[tid, 2] = local_min
            if local_max > stats[tid, 3]:
                stats[tid, 3] = local_max
            stats[tid, 4] += local_count
            stats[tid, 5] += local_above
            seen[tid] = 1
    elif schedule == "guided":
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="guided", chunksize=1):
            tid = threadid()
            start = chunk * BLOCK_ROWS
            end = min(start + BLOCK_ROWS, n)
            local_count = 0
            local_above = 0
            local_sum = 0.0
            local_sumsq = 0.0
            local_min = 1.7976931348623157e308
            local_max = -1.7976931348623157e308
            for i in range(start, end):
                value = x[i]
                local_count = local_count + 1
                local_sum = local_sum + value
                local_sumsq = local_sumsq + value * value
                if value < local_min:
                    local_min = value
                if value > local_max:
                    local_max = value
                if value > threshold:
                    local_above = local_above + 1
                group_counts[tid, c[i]] += 1
                group_sums[tid, c[i]] += value
            stats[tid, 0] += local_sum
            stats[tid, 1] += local_sumsq
            if local_min < stats[tid, 2]:
                stats[tid, 2] = local_min
            if local_max > stats[tid, 3]:
                stats[tid, 3] = local_max
            stats[tid, 4] += local_count
            stats[tid, 5] += local_above
            seen[tid] = 1
    else:
        for chunk in prange(n_chunks, nogil=True, num_threads=threads, schedule="static"):
            tid = threadid()
            start = chunk * BLOCK_ROWS
            end = min(start + BLOCK_ROWS, n)
            local_count = 0
            local_above = 0
            local_sum = 0.0
            local_sumsq = 0.0
            local_min = 1.7976931348623157e308
            local_max = -1.7976931348623157e308
            for i in range(start, end):
                value = x[i]
                local_count = local_count + 1
                local_sum = local_sum + value
                local_sumsq = local_sumsq + value * value
                if value < local_min:
                    local_min = value
                if value > local_max:
                    local_max = value
                if value > threshold:
                    local_above = local_above + 1
                group_counts[tid, c[i]] += 1
                group_sums[tid, c[i]] += value
            stats[tid, 0] += local_sum
            stats[tid, 1] += local_sumsq
            if local_min < stats[tid, 2]:
                stats[tid, 2] = local_min
            if local_max > stats[tid, 3]:
                stats[tid, 3] = local_max
            stats[tid, 4] += local_count
            stats[tid, 5] += local_above
            seen[tid] = 1

    for t in range(threads):
        total += stats[t, 0]
        sumsq += stats[t, 1]
        total_count += <long long>stats[t, 4]
        above_count += <long long>stats[t, 5]
        if stats[t, 4] > 0.0:
            if stats[t, 2] < minimum:
                minimum = stats[t, 2]
            if stats[t, 3] > maximum:
                maximum = stats[t, 3]
        if seen[t]:
            actual += 1
    if total_count == 0:
        minimum = 0.0
        maximum = 0.0
    return (total_count, total, sumsq, minimum, maximum, above_count,
            np.sum(partial_counts[:, :category_count], axis=0).astype(np.int64),
            np.sum(partial_sums[:, :category_count], axis=0).astype(np.float64), actual)
