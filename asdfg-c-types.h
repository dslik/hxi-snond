#include <time.h>           // POSIX time_t type, used by int64_from_time and time_from_int64
#include <sys/types.h>		// POSIX ssize_t type, used by int64_from_ssize and ssize_from_int64
#include <sys/socket.h>		// POSIX socklen_t type, used by size_from_socklen and socklen_from_size

int32_t int32_from_int(int input);
int int_from_int32(int32_t input);

int64_t int64_from_ssize(ssize_t input);
ssize_t ssize_from_int64(int64_t input);

int64_t int64_from_time(time_t input);
time_t time_from_int64(int64_t input);

int64_t int64_from_long(long input);
long long_from_int64(int64_t input);

size_t size_from_socklen(socklen_t input);
socklen_t socklen_from_size(size_t input);

