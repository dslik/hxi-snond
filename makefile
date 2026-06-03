CFLAGS += -Wall -ggdb
LDLIBS += -lm

TARGET = hxi-snond

all: $(TARGET)

clean:
	$(RM) $(TARGET)

.PHONY: all clean
