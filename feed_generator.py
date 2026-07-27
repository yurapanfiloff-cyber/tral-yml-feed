#!/usr/bin/env python3
"""Generate an atomic YML feed from the Tilda catalogue block on tral-diler.ru."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import html
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import zlib
from pathlib import Path
from typing import Any, Iterable

import requests
from lxml import etree, html as lxml_html
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


VERSION = "1.2.3"
DEFAULT_CONFIG = Path(__file__).with_name("config.json")
USER_AGENT = "TralDilerFeedBot/1.0 (+https://tral-diler.ru/)"
SPACE_RE = re.compile(r"\s+")
KNOWN_VENDORS = (
    "TONGYADA",
    "SHENGRUN",
    "LUXUDA",
    "JUYUN",
    "AMUR",
    "CIMC",
)


class FeedError(RuntimeError):
    """A validation error that must prevent publication."""


@dataclasses.dataclass(frozen=True)
class CatalogueBlock:
    recid: str
    storepart_uid: str
    page_size: int


@dataclasses.dataclass
class Product:
    source: dict[str, Any]
    source_url: str
    final_url: str = ""
    status_code: int = 0
    title: str = ""
    description: str = ""
    category_slug: str = ""
    category_name: str = ""
    vendor: str = ""
    model: str = ""
    type_prefix: str = ""
    price: str = ""
    pictures: list[str] = dataclasses.field(default_factory=list)
    params: dict[str, str] = dataclasses.field(default_factory=dict)
    error: str = ""

    @property
    def offer_id(self) -> str:
        path = urllib.parse.urlsplit(self.source_url).path.rstrip("/")
        return urllib.parse.unquote(path.rsplit("/", 1)[-1]).lower()

    @property
    def category_id(self) -> str:
        value = zlib.crc32(self.category_slug.encode("utf-8")) & 0x7FFFFFFF
        return str(value or 1)


def compact_text(value: str | None) -> str:
    if not value:
        return ""
    return SPACE_RE.sub(" ", html.unescape(value)).strip()


def node_text(node: etree._Element | None) -> str:
    if node is None:
        return ""
    texts = node.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]")
    return compact_text(" ".join(str(value) for value in texts))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FeedError(f"Не найден файл конфигурации: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FeedError(f"Ошибка JSON в {path}: {exc}") from exc

    required = ("source_page", "output_path", "status_path", "log_path")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise FeedError("В конфигурации не заполнены поля: " + ", ".join(missing))
    return config


def create_session(timeout: float) -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.0,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET", "HEAD")),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
    )
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


def get(session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
    timeout = kwargs.pop("timeout", getattr(session, "request_timeout", 30.0))
    return session.get(url, timeout=timeout, **kwargs)


def discover_catalogue_block(session: requests.Session, source_page: str) -> CatalogueBlock:
    response = get(session, source_page)
    if response.status_code != 200:
        raise FeedError(f"Страница-источник вернула HTTP {response.status_code}: {source_page}")
    document = lxml_html.fromstring(response.content, base_url=source_page)
    headings = document.xpath(
        "//h1|//h2|//h3"
    )
    target = next(
        (
            node
            for node in headings
            if "модели полуприцепов" in node_text(node).lower()
            and "тралов" in node_text(node).lower()
        ),
        None,
    )
    if target is None:
        raise FeedError("Не найден заголовок блока «Модели полуприцепов и тралов»")

    record = target
    while record is not None and not (record.get("id") or "").startswith("rec"):
        record = record.getparent()
    if record is None:
        raise FeedError("Не удалось определить запись Tilda для заголовка блока")

    candidates = record.xpath(
        "following-sibling::div[starts-with(@id,'rec')][.//*[contains(concat(' ',normalize-space(@class),' '),' js-catalog ')]][1]"
    )
    if not candidates:
        raise FeedError("После заголовка не найден каталог товаров Tilda")
    catalogue = candidates[0]
    recid = (catalogue.get("id") or "").removeprefix("rec")
    scripts = "\n".join(catalogue.xpath(".//script/text()"))
    uid_match = re.search(r"storepart\s*:\s*['\"](\d+)['\"]", scripts)
    size_match = re.search(r"size\s*:\s*(\d+)", scripts)
    if not uid_match or not recid:
        raise FeedError("Не удалось определить storepart и recid товарного блока")
    return CatalogueBlock(
        recid=recid,
        storepart_uid=uid_match.group(1),
        page_size=int(size_match.group(1)) if size_match else 36,
    )


def fetch_catalogue_products(
    session: requests.Session, endpoint: str, block: CatalogueBlock
) -> tuple[list[dict[str, Any]], int]:
    products: list[dict[str, Any]] = []
    page: int | None = 1
    expected_total: int | None = None
    seen_pages: set[int] = set()
    source_categories: dict[int, str] = {}

    while page is not None:
        if page in seen_pages:
            raise FeedError(f"Tilda вернула зацикленную пагинацию: страница {page}")
        seen_pages.add(page)
        params: dict[str, Any] = {
            "storepartuid": block.storepart_uid,
            "recid": block.recid,
            "c": int(time.time() * 1000),
            "getoptions": "true",
            "size": block.page_size,
        }
        if page == 1:
            params["getcurrentpart"] = "true"
        else:
            params["slice"] = page
        response = get(session, endpoint, params=params)
        if response.status_code != 200:
            raise FeedError(f"Каталог Tilda вернул HTTP {response.status_code}, страница {page}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeedError(f"Каталог Tilda вернул не JSON, страница {page}") from exc
        batch = payload.get("products")
        if not isinstance(batch, list):
            raise FeedError(f"В ответе Tilda нет массива products, страница {page}")
        current_total = int(payload.get("total", 0))
        if expected_total is None:
            expected_total = current_total
            for filter_item in (payload.get("filters") or {}).get("filters", []):
                if filter_item.get("name") != "storepartuid":
                    continue
                for value in filter_item.get("values", []):
                    try:
                        source_categories[int(value["id"])] = compact_text(str(value["value"]))
                    except (KeyError, TypeError, ValueError):
                        continue
        elif current_total != expected_total:
            raise FeedError(
                f"Количество товаров изменилось во время обхода: {expected_total} → {current_total}"
            )
        products.extend(batch)
        next_page = payload.get("nextslice")
        page = int(next_page) if next_page not in (None, "", False) else None

    if expected_total is None or len(products) != expected_total:
        raise FeedError(
            f"Получено {len(products)} товаров, но Tilda сообщает {expected_total or 0}"
        )
    urls = [compact_text(str(item.get("url") or "")) for item in products]
    if any(not value for value in urls):
        raise FeedError("В списке Tilda есть товар без URL")
    if len(urls) != len(set(urls)):
        raise FeedError("В списке Tilda обнаружены повторяющиеся URL")
    for product in products:
        raw_partuids = product.get("partuids")
        try:
            partuids = json.loads(raw_partuids) if isinstance(raw_partuids, str) else raw_partuids
        except ValueError:
            partuids = []
        matches = [int(uid) for uid in (partuids or []) if int(uid) in source_categories]
        if matches:
            product["_source_category_uid"] = str(matches[-1])
            product["_source_category_name"] = source_categories[matches[-1]]
    return products, expected_total


def html_key_values(fragment: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not fragment:
        return result
    try:
        root = lxml_html.fragment_fromstring(fragment, create_parent="div")
    except (etree.ParserError, ValueError):
        return result
    for item in root.xpath(".//li"):
        text = node_text(item)
        if ":" not in text:
            continue
        key, value = (compact_text(part) for part in text.split(":", 1))
        if key and value and len(key) <= 80:
            result.setdefault(key, value)
    return result


def table_key_values(document: etree._Element) -> dict[str, str]:
    result: dict[str, str] = {}
    rows = document.xpath(
        "//div[contains(concat(' ',normalize-space(@class),' '),' t614__middle_item ')]"
    )
    for row in rows:
        left = row.xpath(
            ".//div[contains(concat(' ',normalize-space(@class),' '),' t614__left ')]"
            "//*[contains(concat(' ',normalize-space(@class),' '),' t614__middle_title ')][1]"
        )
        right = row.xpath(
            ".//div[contains(concat(' ',normalize-space(@class),' '),' t614__col ')]"
            "//*[contains(concat(' ',normalize-space(@class),' '),' t614__middle_title ')][1]"
        )
        key = node_text(left[0]) if left else ""
        value = node_text(right[0]) if right else ""
        key = key.replace("Грузо- подъёмность", "Грузоподъёмность")
        if key and value and len(key) <= 80:
            result.setdefault(key, value)
    return result


def first_param(params: dict[str, str], names: Iterable[str]) -> str:
    lowered = {compact_text(key).lower().replace("ё", "е"): value for key, value in params.items()}
    for name in names:
        value = lowered.get(name.lower().replace("ё", "е"))
        if value:
            return compact_text(value)
    return ""


def extract_vendor(title: str, params: dict[str, str], source_brand: str = "") -> str:
    if source_brand:
        upper_brand = source_brand.upper()
        for vendor in KNOWN_VENDORS:
            if re.search(rf"\b{re.escape(vendor)}\b", upper_brand):
                return vendor
        return source_brand
    value = first_param(params, ("Марка", "Бренд", "Производитель"))
    upper_value = value.upper()
    for vendor in KNOWN_VENDORS:
        if re.search(rf"\b{re.escape(vendor)}\b", upper_value):
            return vendor
    upper_title = title.upper()
    for vendor in KNOWN_VENDORS:
        if re.search(rf"\b{re.escape(vendor)}\b", upper_title):
            return vendor
    return value.split(" ", 1)[0] if value else ""


def extract_model(title: str, params: dict[str, str], vendor: str) -> str:
    value = first_param(params, ("Модель",))
    if value:
        return value
    mark = first_param(params, ("Марка", "Бренд"))
    if mark and vendor and mark.upper().startswith(vendor.upper()):
        candidate = compact_text(mark[len(vendor) :])
        if candidate:
            return candidate
    if vendor:
        tail = re.split(rf"\b{re.escape(vendor)}\b", title, flags=re.IGNORECASE, maxsplit=1)
        if len(tail) == 2:
            match = re.search(r"\b[A-ZА-Я0-9][A-ZА-Я0-9.-]{2,}\b", tail[1])
            if match:
                return match.group(0)
    return ""


def parse_gallery(source: dict[str, Any], og_image: str) -> list[str]:
    pictures: list[str] = []
    raw_gallery = source.get("gallery")
    if isinstance(raw_gallery, str) and raw_gallery:
        try:
            raw_gallery = json.loads(raw_gallery)
        except ValueError:
            raw_gallery = []
    if isinstance(raw_gallery, list):
        for item in raw_gallery:
            if isinstance(item, dict) and item.get("img"):
                pictures.append(compact_text(str(item["img"])))
    source_img = source.get("img")
    if isinstance(source_img, str) and source_img:
        pictures.append(compact_text(source_img))
    if og_image:
        pictures.append(og_image)
    clean: list[str] = []
    for picture in pictures:
        parsed = urllib.parse.urlsplit(picture)
        if parsed.scheme == "https" and parsed.netloc and picture not in clean:
            clean.append(picture)
    return clean[:10]


def parse_category(
    document: etree._Element, url: str, source: dict[str, Any]
) -> tuple[str, str]:
    product_path = urllib.parse.urlsplit(url).path.rstrip("/")
    path_parts = [part for part in product_path.split("/") if part]
    source_uid = compact_text(str(source.get("_source_category_uid") or ""))
    source_name = compact_text(str(source.get("_source_category_name") or ""))
    if len(path_parts) == 2 and path_parts[0] == "catalog" and source_uid and source_name:
        return f"tilda-{source_uid}", source_name
    candidates: list[tuple[str, str]] = []
    breadcrumb_anchors = document.xpath(
        "//*[contains(concat(' ',normalize-space(@class),' '),' t758 ')]//a[@href]"
    )
    anchors = breadcrumb_anchors or document.xpath("//a[@href]")
    for anchor in anchors:
        href = urllib.parse.urljoin(url, anchor.get("href"))
        path = urllib.parse.urlsplit(href).path.rstrip("/")
        match = re.fullmatch(r"/catalog/([^/]+)", path)
        if match and path != product_path:
            name = node_text(anchor)
            if name:
                candidates.append((match.group(1).lower(), name))
    if candidates:
        return candidates[-1]
    parts = path_parts
    slug = parts[-2].lower() if len(parts) >= 3 and parts[-3] == "catalog" else "catalog"
    if source_uid and source_name:
        return f"tilda-{source_uid}", source_name
    return slug, slug.replace("-", " ").title()


def parse_product(source: dict[str, Any], session: requests.Session) -> Product:
    url = compact_text(str(source.get("url") or ""))
    product = Product(source=source, source_url=url)
    try:
        response = get(session, url)
        product.status_code = response.status_code
        product.final_url = response.url
        if response.status_code != 200:
            raise FeedError(f"HTTP {response.status_code}")
        document = lxml_html.fromstring(response.content, base_url=url)
        h1 = document.xpath("//h1[normalize-space(.)][1]")
        source_title = compact_text(str(source.get("title") or ""))
        product.title = node_text(h1[0]) if h1 else source_title
        if not product.title:
            raise FeedError("нет названия")

        meta_description = document.xpath(
            "//meta[translate(@name,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='description']/@content"
        )
        product.description = compact_text(meta_description[0]) if meta_description else ""
        if not product.description:
            product.description = compact_text(
                " ".join(lxml_html.fragment_fromstring(
                    str(source.get("descr") or ""), create_parent="div"
                ).itertext())
            )
        product.description = product.description[:3000]

        source_params = html_key_values(str(source.get("descr") or ""))
        card_params = table_key_values(document)
        params = dict(source_params)
        params.update(card_params)
        product.params = params
        product.vendor = extract_vendor(
            product.title, params, compact_text(str(source.get("brand") or ""))
        )
        product.model = extract_model(product.title, params, product.vendor)
        product.category_slug, product.category_name = parse_category(document, url, source)
        product.type_prefix = first_param(params, ("Тип", "Тип прицепа")) or product.category_name

        raw_price = source.get("price")
        if raw_price not in (None, ""):
            try:
                price = float(str(raw_price).replace(" ", "").replace(",", "."))
                if price > 0:
                    product.price = f"{price:.2f}"
            except ValueError:
                pass

        og_image = ""
        og = document.xpath(
            "//meta[translate(@property,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')='og:image']/@content"
        )
        if og:
            og_image = compact_text(og[0])
        product.pictures = parse_gallery(source, og_image)
    except Exception as exc:
        product.error = compact_text(str(exc)) or exc.__class__.__name__
    return product


def collect_products(
    sources: list[dict[str, Any]], workers: int, timeout: float
) -> list[Product]:
    local = threading.local()

    def work(source: dict[str, Any]) -> Product:
        if not hasattr(local, "session"):
            local.session = create_session(timeout)
        return parse_product(source, local.session)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(work, sources))


def evaluate_products(
    products: list[Product], expected_total: int, config: dict[str, Any]
) -> tuple[list[Product], list[dict[str, str]], list[str], list[str]]:
    publishable: list[Product] = []
    excluded: list[dict[str, str]] = []
    fatal_errors: list[str] = []
    warnings: list[str] = []
    if len(products) != expected_total:
        fatal_errors.append(f"Получено карточек: {len(products)}, ожидалось: {expected_total}")

    ids = [item.offer_id for item in products]
    empty_ids = [item.source_url for item in products if not item.offer_id]
    if empty_ids:
        fatal_errors.append(f"Пустой ID у {len(empty_ids)} товаров")
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        fatal_errors.append("Повторяющиеся ID: " + ", ".join(duplicates))

    for item in products:
        prefix = f"{item.offer_id or item.source_url}:"
        if item.error:
            fatal_errors.append(f"{prefix} {item.error}")
            continue
        if item.status_code != 200:
            fatal_errors.append(f"{prefix} HTTP {item.status_code}")
            continue
        if not item.price:
            reason = "нет подтверждённой числовой цены"
            excluded.append(
                {
                    "id": item.offer_id,
                    "url": item.source_url,
                    "title": item.title,
                    "reason": reason,
                }
            )
            warnings.append(f"{prefix} исключён из фида: {reason}")
            continue
        if not item.pictures:
            fatal_errors.append(f"{prefix} нет изображения")
        if not item.title:
            fatal_errors.append(f"{prefix} нет названия")
        if not item.category_slug or not item.category_name:
            fatal_errors.append(f"{prefix} не определена категория")
        if not item.vendor:
            fatal_errors.append(f"{prefix} не определён бренд")
        if not item.model:
            warnings.append(f"{prefix} нет заводского индекса модели; используется упрощённый офер")
        publishable.append(item)

    minimum = int(config.get("minimum_products", 1))
    if len(publishable) < minimum:
        fatal_errors.append(
            f"К публикации подготовлено {len(publishable)} товаров, "
            f"минимально допустимо {minimum}"
        )
    return publishable, excluded, fatal_errors, warnings


def existing_offer_count(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        document = etree.parse(str(path))
        return int(document.xpath("count(/yml_catalog/shop/offers/offer)"))
    except (OSError, ValueError, etree.XMLSyntaxError):
        return None


def count_change(new_count: int, output: Path, config: dict[str, Any]) -> dict[str, Any]:
    previous = existing_offer_count(output)
    if not previous:
        return {
            "previous_count": previous,
            "new_count": new_count,
            "difference": None,
            "drop_percent": 0.0,
            "threshold_percent": float(config.get("max_count_drop_percent", 20.0)),
            "critical": False,
        }
    allowed = float(config.get("max_count_drop_percent", 20.0))
    difference = new_count - previous
    drop = max(0.0, ((previous - new_count) / previous) * 100.0)
    return {
        "previous_count": previous,
        "new_count": new_count,
        "difference": difference,
        "drop_percent": round(drop, 2),
        "threshold_percent": allowed,
        "critical": drop > allowed,
    }


def normalize_axes(value: str) -> str:
    match = re.search(r"\d+", value)
    if not match:
        return compact_text(value)
    number = int(match.group(0))
    suffix = "ось" if number == 1 else "оси" if 2 <= number <= 4 else "осей"
    return f"{number} {suffix}"


def normalize_platform_length(value: str) -> str:
    text = compact_text(value)
    match = re.search(r"([\d\s]+)\s*мм\b", text, flags=re.IGNORECASE)
    if not match:
        return text
    millimetres = int(re.sub(r"\s+", "", match.group(1)))
    if millimetres >= 1000 and millimetres % 1000 == 0:
        return f"{millimetres // 1000} м"
    if millimetres >= 1000:
        return f"{millimetres / 1000:g} м"
    return text


def normalize_capacity(value: str) -> str:
    text = compact_text(value)
    match = re.search(r"([\d\s]+)\s*кг\b", text, flags=re.IGNORECASE)
    if not match:
        return text
    kilograms = int(re.sub(r"\s+", "", match.group(1)))
    if kilograms >= 1000 and kilograms % 1000 == 0:
        return f"{kilograms // 1000} тонн"
    return text


def simplified_offer_name(product: Product) -> str:
    base_parts = [product.type_prefix, product.vendor]
    base = " ".join(part for part in base_parts if part).strip()
    characteristics: list[str] = []

    axes = first_param(product.params, ("Количество осей", "Кол-во осей", "Оси"))
    if axes:
        characteristics.append(normalize_axes(axes))
    capacity = first_param(product.params, ("Грузоподъёмность", "Грузоподъемность"))
    if capacity:
        characteristics.append(normalize_capacity(capacity))
    platform = first_param(product.params, ("Длина рабочей площадки",))
    if platform:
        characteristics.append(f"площадка {normalize_platform_length(platform)}")
    if len(characteristics) < 2:
        dimensions = first_param(product.params, ("Габариты (Д×Ш×В)", "Габариты (ДхШхВ)"))
        if dimensions:
            characteristics.append(dimensions)

    name = base
    if characteristics:
        name += ", " + ", ".join(characteristics[:3])
    return compact_text(name) or product.title


def add_text(parent: etree._Element, tag: str, value: str) -> etree._Element:
    node = etree.SubElement(parent, tag)
    node.text = value
    return node


def build_xml(products: list[Product], config: dict[str, Any]) -> bytes:
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=int(config.get("timezone_offset", 3)))))
    root = etree.Element("yml_catalog", date=now.strftime("%Y-%m-%d %H:%M"))
    shop = etree.SubElement(root, "shop")
    add_text(shop, "name", str(config.get("shop_name", "Трал-Дилер")))
    add_text(shop, "company", str(config.get("company_name", "Трал-Дилер")))
    add_text(shop, "url", str(config.get("shop_url", "https://tral-diler.ru/")))
    currencies = etree.SubElement(shop, "currencies")
    etree.SubElement(currencies, "currency", id="RUB", rate="1")

    categories = etree.SubElement(shop, "categories")
    category_map: dict[str, tuple[str, str]] = {}
    for product in products:
        category_map[product.category_id] = (product.category_slug, product.category_name)
    for category_id, (_, category_name) in sorted(category_map.items(), key=lambda x: x[1][1]):
        add_text(categories, "category", category_name).set("id", category_id)

    offers = etree.SubElement(shop, "offers")
    for product in products:
        offer = etree.SubElement(offers, "offer", id=product.offer_id, available="true")
        add_text(offer, "url", product.source_url)
        add_text(offer, "price", product.price)
        add_text(offer, "currencyId", "RUB")
        add_text(offer, "categoryId", product.category_id)
        for picture in product.pictures:
            add_text(offer, "picture", picture)
        if product.model:
            add_text(offer, "name", product.title)
            add_text(offer, "typePrefix", product.type_prefix)
            add_text(offer, "vendor", product.vendor)
            add_text(offer, "model", product.model)
        else:
            add_text(offer, "name", simplified_offer_name(product))
        if product.description:
            add_text(offer, "description", product.description)
        for name, value in product.params.items():
            if not name or not value:
                continue
            param = add_text(offer, "param", value[:1000])
            param.set("name", name[:80])

    return etree.tostring(
        root,
        encoding="UTF-8",
        xml_declaration=True,
        pretty_print=True,
        doctype='<!DOCTYPE yml_catalog SYSTEM "shops.dtd">',
    )


def validate_xml(xml_bytes: bytes, expected_count: int) -> None:
    try:
        document = etree.fromstring(xml_bytes)
    except etree.XMLSyntaxError as exc:
        raise FeedError(f"Сформирован невалидный XML: {exc}") from exc
    offers = document.xpath("/yml_catalog/shop/offers/offer")
    if len(offers) != expected_count:
        raise FeedError(f"В XML {len(offers)} оферов, ожидалось {expected_count}")
    category_ids = set(document.xpath("/yml_catalog/shop/categories/category/@id"))
    offer_ids: set[str] = set()
    for offer in offers:
        offer_id = offer.get("id") or ""
        if not offer_id or offer_id in offer_ids:
            raise FeedError(f"В XML пустой или повторяющийся ID: {offer_id}")
        offer_ids.add(offer_id)
        for tag in ("url", "price", "currencyId", "categoryId", "picture", "name"):
            value = offer.findtext(tag)
            if not value or not value.strip():
                raise FeedError(f"Офер {offer_id}: отсутствует {tag}")
        if offer.findtext("categoryId") not in category_ids:
            raise FeedError(f"Офер {offer_id}: неизвестная categoryId")


def product_report(product: Product, included: bool) -> dict[str, Any]:
    return {
        "id": product.offer_id,
        "url": product.source_url,
        "http_status": product.status_code,
        "title": product.title,
        "price": product.price or None,
        "pictures": len(product.pictures),
        "vendor": product.vendor or None,
        "model": product.model or None,
        "category": product.category_name or None,
        "params": len(product.params),
        "offer_type": "combined" if product.model else "simplified",
        "feed_name": product.title if product.model else simplified_offer_name(product),
        "included_in_feed": included,
        "error": product.error or None,
    }


def write_status(path: Path, status: str, **values: Any) -> None:
    payload = {
        "status": status,
        "generator_version": VERSION,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        **values,
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def configure_logging(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=(logging.FileHandler(path, encoding="utf-8"), logging.StreamHandler()),
    )


def run(config: dict[str, Any], audit_only: bool, force_publish: bool) -> int:
    output = Path(config["output_path"])
    status_path = Path(config["status_path"])
    report_path = Path(config.get("audit_report_path", status_path.with_name("audit.json")))
    timeout = float(config.get("request_timeout_seconds", 35))
    workers = max(1, min(int(config.get("workers", 6)), 10))
    session = create_session(timeout)

    logging.info("Определение товарного блока на %s", config["source_page"])
    block = discover_catalogue_block(session, config["source_page"])
    logging.info("Найден блок rec%s, storepart %s", block.recid, block.storepart_uid)
    sources, expected_total = fetch_catalogue_products(
        session,
        str(config.get("tilda_endpoint", "https://store.tildaapi.com/api/getproductslist/")),
        block,
    )
    logging.info("В блоке найдено товаров: %d", expected_total)
    products = collect_products(sources, workers=workers, timeout=timeout)
    publishable, excluded, fatal_errors, warnings = evaluate_products(
        products, expected_total, config
    )
    published_ids = {product.offer_id for product in publishable}
    exclusion_percent = (len(excluded) / expected_total * 100.0) if expected_total else 0.0
    change = count_change(len(publishable), output, config)
    if change["previous_count"] and change["new_count"] < change["previous_count"]:
        warnings.append(
            f"Количество оферов уменьшилось с {change['previous_count']} "
            f"до {change['new_count']} ({change['drop_percent']:.2f}%)"
        )
    if change["critical"]:
        message = (
            f"Критическое уменьшение фида: {change['previous_count']} → "
            f"{change['new_count']} ({change['drop_percent']:.2f}%, "
            f"порог {change['threshold_percent']:.2f}%)"
        )
        if force_publish:
            warnings.append(message + "; публикация подтверждена параметром --force-publish")
        else:
            fatal_errors.append(message + "; предыдущий рабочий фид сохранён")

    report = {
        "status": "error" if fatal_errors else "warning" if warnings else "ok",
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_page": config["source_page"],
        "catalogue": dataclasses.asdict(block),
        "expected_total": expected_total,
        "checked_products": len(products),
        "publishable_products": len(publishable),
        "excluded_products_count": len(excluded),
        "excluded_percent": round(exclusion_percent, 2),
        "simplified_offers_count": sum(1 for item in publishable if not item.model),
        "fatal_errors_count": len(fatal_errors),
        "fatal_errors": fatal_errors,
        "warnings_count": len(warnings),
        "warnings": warnings,
        "excluded_products": excluded,
        "count_change": change,
        "force_publish": force_publish,
        "products": [product_report(item, item.offer_id in published_ids) for item in products],
    }
    atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    if fatal_errors:
        write_status(
            status_path,
            "error",
            message="Проверка товаров не пройдена; рабочий фид не изменён",
            source_products=len(products),
            publishable_products=len(publishable),
            fatal_errors_count=len(fatal_errors),
            audit_report=str(report_path),
        )
        raise FeedError(f"Проверка не пройдена: критических ошибок {len(fatal_errors)}")

    xml_bytes = build_xml(publishable, config)
    validate_xml(xml_bytes, len(publishable))
    if audit_only:
        write_status(
            status_path,
            "warning" if warnings else "ok",
            message="Аудит пройден; публикация не выполнялась",
            source_products=len(products),
            publishable_products=len(publishable),
            excluded_products=len(excluded),
            simplified_offers=sum(1 for item in publishable if not item.model),
            audit_report=str(report_path),
        )
        logging.info("Аудит завершён без публикации")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    candidate = output.with_name(f".{output.name}.candidate")
    candidate.write_bytes(xml_bytes)
    validate_xml(candidate.read_bytes(), len(publishable))
    if output.exists():
        backup = output.with_name(output.name + ".previous")
        shutil.copy2(output, backup)
    os.replace(candidate, output)
    digest = hashlib.sha256(xml_bytes).hexdigest()
    write_status(
        status_path,
        "warning" if warnings else "ok",
        message=(
            "Фид опубликован с исключениями и предупреждениями"
            if warnings
            else "Фид успешно опубликован"
        ),
        source_products=len(products),
        published_products=len(publishable),
        excluded_products=len(excluded),
        simplified_offers=sum(1 for item in publishable if not item.model),
        output_path=str(output),
        sha256=digest,
        audit_report=str(report_path),
    )
    logging.info("Опубликован фид: %s, товаров: %d", output, len(publishable))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Генератор YML-фида Трал-Дилер")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--audit-only", action="store_true", help="Проверить источник без публикации")
    parser.add_argument(
        "--force-publish",
        action="store_true",
        help="Подтвердить публикацию при критическом уменьшении числа оферов",
    )
    args = parser.parse_args()
    config: dict[str, Any] = {}
    try:
        config = load_config(args.config)
        configure_logging(Path(config["log_path"]))
        lock_path = Path(config.get("lock_path", "/tmp/tral-yml-feed.lock"))
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FeedError("Предыдущий запуск ещё не завершён") from exc
            return run(config, args.audit_only, args.force_publish)
    except FeedError as exc:
        logging.error("%s", exc)
        if config.get("status_path"):
            with contextlib.suppress(Exception):
                write_status(
                    Path(config["status_path"]),
                    "error",
                    message=str(exc),
                )
        return 1
    except Exception:
        logging.exception("Непредвиденная ошибка; рабочий фид не изменён")
        if config.get("status_path"):
            with contextlib.suppress(Exception):
                write_status(
                    Path(config["status_path"]),
                    "error",
                    message="Непредвиденная ошибка; рабочий фид не изменён",
                )
        return 2


if __name__ == "__main__":
    sys.exit(main())
