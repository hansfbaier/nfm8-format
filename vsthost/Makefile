CC = gcc
CFLAGS ?= -O2 -std=c11 -Wall -Wextra
TARGET = vsthost.exe

all: $(TARGET)

$(TARGET): vsthost.c
	$(CC) $(CFLAGS) -o $@ $< -s

clean:
	rm -f $(TARGET)

.PHONY: all clean
