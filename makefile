CFLAGS += -Wall -ggdb
LDLIBS += -lm

TARGET = hxi-snond
OBJS   = hxi-snond.o cJSON.o

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

clean:
	$(RM) $(TARGET) $(OBJS)

.PHONY: all clean