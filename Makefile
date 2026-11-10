# Any distribution: make install (PREFIX=/usr/local by default), make uninstall, make check.
# Packagers: make install PREFIX=/usr DESTDIR="$pkgdir"
PREFIX ?= /usr/local
ID = io.github.rdpdesk
FILES = $(DESTDIR)$(PREFIX)/bin/rdpdesk \
        $(DESTDIR)$(PREFIX)/share/applications/$(ID).desktop \
        $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/$(ID).svg \
        $(DESTDIR)$(PREFIX)/share/mime/packages/$(ID).xml

install:
	install -Dm755 rdpdesk.py   $(DESTDIR)$(PREFIX)/bin/rdpdesk
	install -Dm644 $(ID).desktop $(DESTDIR)$(PREFIX)/share/applications/$(ID).desktop
	install -Dm644 $(ID).svg     $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/$(ID).svg
	install -Dm644 $(ID).xml     $(DESTDIR)$(PREFIX)/share/mime/packages/$(ID).xml
	$(if $(DESTDIR),,$(MAKE) caches)

uninstall:
	rm -f $(FILES)
	$(if $(DESTDIR),,$(MAKE) caches)

# Package managers run these from their own hooks; a plain `make install` has to do it itself.
caches:
	-update-mime-database $(PREFIX)/share/mime
	-update-desktop-database -q $(PREFIX)/share/applications
	-gtk-update-icon-cache -q -t $(PREFIX)/share/icons/hicolor

check:
	python3 test_rdpdesk.py

.PHONY: install uninstall caches check
