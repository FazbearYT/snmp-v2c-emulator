# Эмулятор SNMP v2c-устройства

Конфигурируемый SNMP v2c command responder с движком сценарного изменения
метрик. Эмулятор позволяет тестировать системы мониторинга без физического
сетевого оборудования и воспроизводить рост нагрузки, переключение состояний и
циклические последовательности значений.

## Возможности MVP

- SNMP v2c по UDP с настраиваемой community string;
- запросы GET, GETNEXT и GETBULK;
- типы Integer, OctetString, ObjectIdentifier, IpAddress, Counter32, Gauge32,
  TimeTicks и Counter64;
- упорядоченное виртуальное дерево OID;
- YAML-профили с проверкой схемы;
- сценарии set, sequence, ramp и step;
- повторяющиеся сценарии;
- проверка конфигурации без запуска агента.

## Быстрый старт

Для запуска используется `uv`. Версия Python 3.13 закреплена в
`.python-version`, а точные версии зависимостей — в `uv.lock`.

```bash
uv sync
uv run snmp-emulator --config examples/device.yaml
```

Для установки тестовых инструментов:

```bash
uv sync --extra test
```

Активировать `.venv` вручную не требуется: `uv run` запускает команду в
окружении проекта.

Пример использует UDP-порт 1161, поэтому для запуска не требуются повышенные
привилегии.

## Проверка

```bash
snmpget -v2c -c public 127.0.0.1:1161 1.3.6.1.4.1.55555.1.1.0
snmpwalk -v2c -c public 127.0.0.1:1161 1.3.6.1
snmpbulkwalk -v2c -c public 127.0.0.1:1161 1.3.6.1
```

Проверить YAML без открытия UDP-порта:

```bash
uv run snmp-emulator --config examples/device.yaml --check-config
```

## Конфигурация

```yaml
schema_version: 1
agent:
  host: 0.0.0.0
  port: 1161
  community: public
metrics:
  - name: cpu_usage
    oid: 1.3.6.1.4.1.55555.1.1.0
    type: Gauge32
    initial: 15
scenarios:
  - name: overload
    repeat_every: 60
    actions:
      - type: ramp
        metric: cpu_usage
        at: 5
        duration: 40
        start: 15
        end: 95
      - type: set
        metric: cpu_usage
        at: 50
        value: 15
```

Временные значения задаются в секундах. `repeat_every` начинает сценарий заново
через указанный интервал.

## Действия сценариев

`set` устанавливает значение начиная с момента `at`.

`sequence` последовательно выбирает элементы `values` с интервалом `interval`.

`ramp` линейно изменяет значение от `start` до `end` за `duration` секунд.

`step` изменяет значение на `amount` через каждый `interval` и поддерживает
ограничения `minimum` и `maximum`.

## Тесты

```bash
uv run --extra test pytest
```

Набор включает модульные проверки доменной модели, конфигурации, сценариев и
сетевой тест SNMP-запроса через UDP.

## Ограничения MVP

Текущая версия не поддерживает SNMP SET, traps, SNMP v3, импорт ASN.1 MIB и
одновременную эмуляцию нескольких устройств. Эти возможности могут быть
добавлены отдельными адаптерами после уточнения требований.
