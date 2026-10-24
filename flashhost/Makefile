BUILD ?= ../build

SOURCES = flashhost.c http_cache.c

$(BUILD)/flashhost.exe: $(SOURCES) http_cache.h
	mkdir -p $(BUILD)
	winegcc -O2 -Wall -mconsole -o $@ $(SOURCES) -latl -lole32 -loleaut32 -luuid -lurlmon -lwininet -luser32 -lgdi32

clean:
	rm -f $(BUILD)/flashhost.exe $(BUILD)/flashhost.exe.so

.PHONY: clean
