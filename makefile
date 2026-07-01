CFLAGS  += -Wall -Wextra -Werror -pedantic -ggdb -fno-omit-frame-pointer
LDFLAGS += -rdynamic
LDLIBS  += -lm

TARGET      = hxi-snond gpm8310-snond rfpdu-snond
TESTS       = asdfg-c-types-tests asdfg-c-uuid-tests
SHARED_OBJS = asdfg-c-dbc.o asdfg-c-types.o asdfg-c-uuid.o
HXI_OBJS    = hxi-snond.o hxi-config.o cJSON.o $(SHARED_OBJS)
GPM_OBJS    = gpm8310-snond.o cJSON.o $(SHARED_OBJS)
RFP_OBJS    = rfpdu-snond.o cJSON.o $(SHARED_OBJS)
TEST_OBJS   = asdfg-c-types-tests.o $(SHARED_OBJS)

all: $(TARGET) $(TESTS)

hxi-snond: $(HXI_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

gpm8310-snond: $(GPM_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

rfpdu-snond: $(RFP_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

$(TESTS): $(TEST_OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

%.o: %.html
	$(CC) $(CFLAGS) -x c -c $< -o $@

clean:
	$(RM) $(TARGET) $(TESTS) $(HXI_OBJS) $(GPM_OBJS) $(RFP_OBJS) $(TEST_OBJS)

.PHONY: all clean