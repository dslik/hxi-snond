CFLAGS  += -Wall -Wextra -Werror -pedantic -ggdb -fno-omit-frame-pointer
LDFLAGS += -rdynamic
LDLIBS  += -lm

TARGET = hxi-snond
OBJS   = hxi-snond.o hxi-config.o dbc.o cJSON.o 

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CC) $(CFLAGS) $(LDFLAGS) $^ $(LDLIBS) -o $@

%.o: %.html
	$(CC) $(CFLAGS) -x c -c $< -o $@

clean:
	$(RM) $(TARGET) $(OBJS)

.PHONY: all clean