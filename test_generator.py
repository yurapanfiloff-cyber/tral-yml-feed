import tempfile
import unittest
from pathlib import Path

from lxml import etree

from feed_generator import (
    Product,
    build_xml,
    count_change,
    evaluate_products,
    simplified_offer_name,
    validate_xml,
)


def product(**values):
    defaults = {
        "source": {},
        "source_url": "https://tral-diler.ru/catalog/traly3osi/tongyada-trawl",
        "final_url": "https://tral-diler.ru/catalog/traly3osi/tongyada-trawl",
        "status_code": 200,
        "title": "Среднерамный трал TONGYADA",
        "description": "Описание",
        "category_slug": "traly3osi",
        "category_name": "3-х осные тралы",
        "vendor": "TONGYADA",
        "model": "",
        "type_prefix": "Среднерамный трал",
        "price": "5450000.00",
        "pictures": ["https://static.tildacdn.com/test.webp"],
        "params": {
            "Количество осей": "3 х 16000 кг",
            "Грузоподъёмность": "60 000 кг",
            "Длина рабочей площадки": "13 000 мм",
        },
    }
    defaults.update(values)
    return Product(**defaults)


class GeneratorTests(unittest.TestCase):
    def test_simplified_offer_has_full_name_and_no_model_fields(self):
        item = product()
        self.assertEqual(
            simplified_offer_name(item),
            "Среднерамный трал TONGYADA, 3 оси, 60 тонн, площадка 13 м",
        )
        xml = build_xml([item], {})
        validate_xml(xml, 1)
        offer = etree.fromstring(xml).xpath("/yml_catalog/shop/offers/offer")[0]
        self.assertIsNone(offer.get("type"))
        self.assertIsNone(offer.find("model"))
        self.assertIsNone(offer.find("vendor"))
        self.assertIsNone(offer.find("typePrefix"))

    def test_missing_price_is_excluded_but_missing_model_is_allowed(self):
        included = product()
        excluded = product(
            source_url="https://tral-diler.ru/catalog/traly7osnyye/amur-tral-dlya-yacht",
            price="",
        )
        publishable, exclusions, fatal, warnings = evaluate_products(
            [included, excluded], 2, {"minimum_products": 1}
        )
        self.assertEqual([included], publishable)
        self.assertEqual(1, len(exclusions))
        self.assertIn("нет подтверждённой числовой цены", exclusions[0]["reason"])
        self.assertFalse(fatal)
        self.assertTrue(warnings)

    def test_count_drop_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "direct.yml"
            root = etree.Element("yml_catalog")
            shop = etree.SubElement(root, "shop")
            offers = etree.SubElement(shop, "offers")
            for index in range(92):
                etree.SubElement(offers, "offer", id=str(index))
            path.write_bytes(etree.tostring(root))

            allowed = count_change(78, path, {"max_count_drop_percent": 20})
            critical = count_change(70, path, {"max_count_drop_percent": 20})
            self.assertEqual(15.22, allowed["drop_percent"])
            self.assertFalse(allowed["critical"])
            self.assertEqual(23.91, critical["drop_percent"])
            self.assertTrue(critical["critical"])


if __name__ == "__main__":
    unittest.main()
