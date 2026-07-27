# Проверка источника фида tral-diler.ru

Дата проверки: 20 июля 2026 года.

## Итог

- товарный блок найден автоматически;
- идентификатор блока Tilda: `rec2320492521`;
- раздел Tilda: `354504170132`;
- товаров в блоке: 92;
- проверено карточек: 92;
- карточек с HTTP-статусом не 200: 0;
- карточек без изображения: 0;
- карточек без бренда: 0;
- карточек без категории: 0;
- карточек без цены: 14;
- карточек без модели: 3;
- товаров, подготовленных к публикации: 78;
- товаров, исключённых из фида из-за отсутствия цены: 14;
- упрощённых оферов без `<model>`: 2;
- критических ошибок: 0.

Проверка пройдена с предупреждениями. Сформирован валидный YML на 78 оферов. Исключение 14 товаров составляет 15,22% от исходных 92 и не превышает установленный порог 20%.

## Карточки без цены

1. [TONGYADA CTY9401TJW30](https://tral-diler.ru/catalog/container/tongyada-cty9401tjw30)
2. [AMUR LYR9400JSD](https://tral-diler.ru/catalog/bortovyye/amur-lyr9400jsd)
3. [AMUR LYR9600JSD](https://tral-diler.ru/catalog/bortovyye/amur-lyr9600jsd)
4. [AMUR LYR9602ZX](https://tral-diler.ru/catalog/samosvalnyye/amur-lyr9602zx30m3)
5. [TONGYADA — самосвал 40 тонн](https://tral-diler.ru/catalog/samosvalnyye/tongyada-samosval-40t)
6. [TONGYADA CTY9603TDPS — 14,5 м](https://tral-diler.ru/catalog/traly3osi/tongyada-cty9603tdps-14m5)
7. [AMUR LYR9806TDPW — 90 тонн](https://tral-diler.ru/catalog/traly5osnyye/amur-lyr9806tdpw-4)
8. [CIMC LYR9906TDPL](https://tral-diler.ru/catalog/traly6osnyye/cimc-lyr9906tdpl)
9. [AMUR LYR9906TDPL](https://tral-diler.ru/catalog/traly6osnyye/amur-lyr9906tdpl-2)
10. [AMUR — трал для яхт и катеров](https://tral-diler.ru/catalog/traly7osnyye/amur-tral-dlya-yacht)
11. [TONGYADA CTY9860TDPXZ](https://tral-diler.ru/catalog/tralyosobyye/tongyada-cty9860dpxz)
12. [SHENGRUN — цистерна СУГ/LPG](https://tral-diler.ru/catalog/cisterny/shengrun-lpg-50-60)
13. [AMUR LYR9806TDPW — 5 осей](https://tral-diler.ru/catalog/traly5osnyye/amur-lyr9806tdpw-10)
14. [TONGYADA — битумовоз](https://tral-diler.ru/catalog/cisterny/tongyada-bitumovoz)

## Упрощённые оферы без заводского индекса модели

В фид попали два товара без выдуманного `<model>`:

1. [Среднерамный трал TONGYADA](https://tral-diler.ru/catalog/traly3osi/tongyada-trawl) — `<name>Среднерамный трал TONGYADA, 3 оси, 60 тонн, площадка 13 м</name>`.
2. [Полуприцеп-контейнеровоз LUXUDA](https://tral-diler.ru/catalog/container/luxuda-konteynerovoz-4-osi) — `<name>Полуприцеп-контейнеровоз LUXUDA, 4 оси, 40 тонн</name>`.

[Трал AMUR для яхт и катеров](https://tral-diler.ru/catalog/traly7osnyye/amur-tral-dlya-yacht) также не имеет заводского индекса модели, но в текущий фид не попал из-за отсутствия цены.

## Что исправить в Tilda

1. У 14 товаров заполнить числовую цену в карточке товара Tilda. Текст «цена по запросу» вместо числа не подходит для обязательного элемента `<price>`.
2. Поле `Модель` заполнять только при наличии достоверного заводского индекса. Описательные значения в `<model>` не подставлять.
3. После публикации изменений повторно запустить генератор с параметром `--audit-only`.
4. Если число оферов уменьшится более чем на 20% относительно рабочего фида, проверить отчёт и только затем при необходимости применить `--force-publish`.

Полные машинные результаты по каждой карточке находятся в `runtime/audit.json`.

## Статус размещения

27 июля 2026 года проект подготовлен к размещению через GitHub Actions и GitHub Pages:

- автоматический запуск настроен один раз в сутки;
- после ручного обновления цен или ассортимента предусмотрен отдельный ручной запуск;
- публикация Pages выполняется только после успешной проверки;
- при ошибке предыдущий опубликованный фид не заменяется;
- критическое уменьшение можно подтвердить только ручным запуском с `force_publish`;
- отчёт, статус и журнал каждого запуска сохраняются на 30 дней;
- подготовлен постоянный адрес `https://feed.tral-diler.ru/direct.yml`.

Публичное размещение ещё не выполнено: требуется создать репозиторий, включить GitHub Pages и добавить DNS-запись `feed` после получения адреса назначения от GitHub.
