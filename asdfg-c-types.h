#include <sys/types.h>
#include <time.h>           // POSIX time_t type, used by int64_from_time and time_from_int64

int32_t int32_from_int(int input);
int int_from_int32(int32_t input);

int64_t int64_from_ssize(ssize_t input);
ssize_t ssize_from_int64(int64_t input);

int64_t int64_from_time(time_t input);
time_t time_from_int64(int64_t input);
