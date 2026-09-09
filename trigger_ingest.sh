#!/bin/bash
# "Пробуждает" файлы, лежащие в папке импорта CWA без нового события.
#
# Служба импорта CWA (cwa-ingest-service) следит за папкой через inotifywait,
# который реагирует только на НОВЫЕ события (создание/перемещение), а не на
# файлы, уже лежащие в папке до старта наблюдателя.
#
# Этот скрипт перемещает каждый файл туда-обратно, чтобы сгенерировать событие
# moved_to и заставить CWA его обработать.
#
# Использование: ./trigger_ingest.sh

set -e

INGEST="~/data/calibre/booksitdwl"
CONTAINER="Books-IT"

if [ ! -d "$INGEST" ]; then
    echo "Папка импорта не найдена: $INGEST" >&2
    exit 1
fi

shopt -s nullglob
files=("$INGEST"/*)
shopt -u nullglob

if [ ${#files[@]} -eq 0 ]; then
    echo "В папке импорта нет файлов."
    exit 0
fi

echo "Пробуждаю ${#files[@]} файл(ов) в $INGEST ..."
for f in "${files[@]}"; do
    [ -e "$f" ] || continue
    name=$(basename "$f")
    # перемещаем внутрь контейнера туда-обратно (событие moved_to)
    docker exec "$CONTAINER" sh -c 'mv "$1" /tmp/._cwa_trigger && mv /tmp/._cwa_trigger "$1"' -- "/cwa-book-ingest/$name"
    echo "triggered: $name"
    sleep 1
done

echo "Готово. Следите за логами: docker logs -f $CONTAINER"
