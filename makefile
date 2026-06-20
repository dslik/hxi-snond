CFLAGS  += -Wall -Wextra -Werror -pedantic -ggdb -fno-omit-frame-pointer
LDFLAGS += -rdynamic
LDLIBS  += -lm

TARGET      = hxi-snond
TESTS       = asdfg-c-types-test
SHARED_OBJS = asdfg-c-dbc.o asdfg-c-types.o
DAEMON_OBJS = hxi-snond.o hxi-config.o cJSON.o $(SHARED_OBJS)
TEST_OBJS   = asdfg-c-types-tests.o $(SHARED_OBJS)

all: $(TARGET) $(TESTS)

$(TARGET): $(DAEMON_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

$(TESTS): $(TEST_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

%.o: %.html
	$(CC) $(CFLAGS) -x c -c $< -o $@

clean:
	$(RM) $(TARGET) $(TESTS) $(DAEMON_OBJS) $(TEST_OBJS)

.PHONY: all clean