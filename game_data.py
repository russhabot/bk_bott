"""Game balance data for Burnt Kingdoms.

Add buildings, units, or technologies here. Existing IDs are kept stable so
players' saved buildings, armies, and research continue to load after updates.
"""

COUNTRIES = """AF AL DZ AD AO AG AR AM AU AT AZ BS BH BD BB BY BE BZ BJ BT BO BA BW BR BN BG BF BI CV KH CM CA CF TD CL CN
CO KM CG CD CR CI HR CU CY CZ DK DJ DM DO EC EG SV GQ ER EE SZ ET FJ FI FR GA GM GE DE GH GR GD GT GN GW GY HT HN HU IS
IN ID IR IQ IE IL IT JM JP JO KZ KE KI KP KR KW KG LA LV LB LS LR LY LI LT MG MW MY MV ML MT MH MR MU MX FM MD MC MN
ME MA MZ MM NA NR NP NL NZ NI NE NG MK NO OM PK PW PS PA PG PY PE PH PL PT QA RO RU RW KN LC VC WS SM ST SA SN RS SC SL
SG SK SI SB SO ZA SS ES LK SD SR SE CH SY TW TJ TZ TH TL TG TO TT TN TR TM TV UG UA AE GB US UY UZ VU VA VE VN YE ZM ZW""".split()


def _building(category, name, cost, **details):
    return {"cat": category, "fa": name, "cost": cost, **details}


# Categories: factory, bank, food, welfare, mil, logistics
BUILDINGS = {
    # Economic buildings repay their setup cost quickly through daily income.
    "f_light": _building("factory", "کارخانه مصنوعات", 60_000, income=85_000),
    "f_steel": _building("factory", "فولاد و ذوب‌آهن", 180_000, income=230_000),
    "f_oil": _building("factory", "پالایشگاه نفت", 450_000, income=540_000),
    "f_chip": _building("factory", "کارخانه الکترونیک", 750_000, income=900_000),
    "bank": _building("bank", "بانک ملی", 25_000, income=36_000),
    "farm_s": _building("food", "مزرعه گندم", 25_000, food=300),
    "farm_l": _building("food", "مزرعه صنعتی", 75_000, food=900),
    "park": _building("welfare", "پارک ملی", 25_000, happy=1.5),
    "fun": _building("welfare", "شهربازی", 65_000, happy=3),
    "mall": _building("welfare", "مرکز خرید", 95_000, happy=3.5),
    "stadium": _building("welfare", "ورزشگاه", 140_000, happy=5),
    "arsenal": _building("mil", "انبار تسلیحات", 45_000, cap=500),
    # These make international trade possible; they do not print money.
    "port": _building("logistics", "بندر تجاری", 45_000, fee_discount=0.015),
    "airport": _building("logistics", "فرودگاه تجاری", 55_000, fee_discount=0.02),
}


def _unit(branch, name, cost, attack, defense, size, factory_cost, daily_rate, **details):
    return {
        "br": branch,
        "fa": name,
        "cost": cost,
        "atk": attack,
        "df": defense,
        "size": size,
        "fcost": factory_cost,
        "rate": daily_rate,
        **details,
    }


# Factory output is units/day per owned military factory; size is arsenal space.
# Legacy IDs (fighter, tank, etc.) remain intact for existing player inventories.
UNITS = {
    "infantry": _unit("land", "پیاده‌نظام", 500, 1, 1, 0.1, 10_000, 50, upkeep=0.2),
    "spy": _unit("land", "جاسوس", 3_000, 0, 0, 0.1, 8_000, 5, upkeep=0.1),
    "hacker": _unit("land", "هکر", 5_000, 0, 0, 0.1, 12_000, 5, upkeep=0.1),
    "antihack": _unit("land", "ضد هکر", 5_000, 0, 5, 0.1, 12_000, 5, upkeep=0.1),
    "jeep": _unit("land", "جیپ نظامی", 6_000, 8, 6, 0.5, 15_000, 5, upkeep=0.5),
    "mine": _unit("land", "مین زمینی", 1_000, 0, 8, 0.2, 10_000, 20, upkeep=0),
    "apc": _unit("land", "نفربر زرهی", 22_000, 18, 24, 1, 55_000, 3, upkeep=1),
    "tank": _unit("land", "تانک اصلی میدان نبرد", 55_000, 42, 36, 1.5, 90_000, 2, upkeep=1.5),
    "artillery": _unit("land", "توپخانه خودکششی", 48_000, 50, 12, 1.5, 80_000, 2, upkeep=1),
    "drone": _unit("land", "پهپاد شناسایی", 18_000, 16, 8, 0.5, 40_000, 4, upkeep=0.3),
    # Air units have distinct price, damage, defense, and production capacity.
    "fighter": _unit("air", "جنگنده نسل چهارم", 75_000, 78, 42, 2, 50_000, 1, upkeep=2),
    "f16": _unit("air", "F-16 Fighting Falcon", 95_000, 92, 48, 2, 65_000, 1, upkeep=2),
    "rafale": _unit("air", "Dassault Rafale", 170_000, 132, 72, 2.5, 100_000, 1, upkeep=2.5),
    "su57": _unit("air", "Su-57 Felon", 220_000, 158, 90, 3, 130_000, 1, upkeep=3),
    "f35": _unit("air", "F-35 Lightning II", 250_000, 175, 110, 3, 150_000, 1, upkeep=3),
    "bomber": _unit("air", "بمب‌افکن راهبردی", 320_000, 230, 65, 4, 180_000, 1, upkeep=4),
    "missile": _unit("air", "موشک بالستیک متعارف", 42_000, 68, 0, 1, 50_000, 3, upkeep=0),
    "aad": _unit("air", "پدافند هوایی", 52_000, 0, 105, 1.5, 35_000, 2, upkeep=0.5),
    "sam_2": _unit("air", "سامانه پدافندی دوربرد", 110_000, 0, 190, 2.5, 75_000, 1, upkeep=1),
    "nuke": _unit("air", "بمب اتم", 5_000_000, 500, 0, 5, 2_500_000, 1, lock="nuke_ok", upkeep=0),
    "warship": _unit("navy", "ناو جنگی", 150_000, 200, 150, 4, 100_000, 1, upkeep=5),
    "destroyer": _unit("navy", "ناوشکن موشک‌انداز", 210_000, 245, 205, 4, 120_000, 1, upkeep=5),
    "submarine": _unit("navy", "زیردریایی تهاجمی", 280_000, 290, 110, 3.5, 150_000, 1, upkeep=4),
    "carrier": _unit("navy", "ناو هواپیمابر", 650_000, 420, 360, 8, 400_000, 1, upkeep=10),
    "seamine": _unit("navy", "مین دریایی", 3_000, 0, 15, 0.3, 15_000, 10, upkeep=0),
}


TECHS = {
    "econ_1": {"fa": "اصلاحات اقتصادی (+۱۰٪ درآمد)", "pp": 30, "money": 80_000, "req": None},
    "agri_1": {"fa": "کشاورزی مدرن (+۲۵٪ غذا)", "pp": 30, "money": 75_000, "req": None},
    "trade_1": {"fa": "راه‌های بازرگانی (کارمزد کمتر)", "pp": 35, "money": 100_000, "req": None},
    "mil_1": {"fa": "نوسازی ارتش (+۱۰٪ قدرت حمله)", "pp": 40, "money": 150_000, "req": None},
    "cyber_1": {"fa": "امنیت سایبری", "pp": 30, "money": 100_000, "req": None},
    "nuc_1": {"fa": "تحقیقات هسته‌ای", "pp": 60, "money": 500_000, "req": None},
    "nuc_2": {"fa": "غنی‌سازی اورانیوم", "pp": 80, "money": 1_500_000, "req": "nuc_1"},
}

START = {"money": 500_000, "pp": 50, "food": 1_000, "happy": 70}
WAR_PP = 30
TRADE_PP = 5
BANK_TRANSFER_LIMIT = 250_000
LOAN_LIMIT = 250_000