"""Константы AMO CRM: ID кастомных полей, enum-значений, статусов воронки.

Все ID специфичны для аккаунта qadam.amocrm.ru. При смене аккаунта —
менять только здесь.
"""

# --- Кастомные поля (field_id) ---
FIELD_CITY = 879211          # «Город» (enum-поле)
FIELD_DEPARTMENT = 912857    # «Отдел» (enum-поле: Offline / Online)
FIELD_UTM_SOURCE = 869441    # UTM_SOURCE (текстовое, дублирует field_code)
FIELD_UTM_MEDIUM = 869443    # UTM_MEDIUM
FIELD_UTM_CAMPAIGN = 869445  # UTM_CAMPAIGN
FIELD_UTM_CONTENT = 869447   # UTM_CONTENT (кладём DID: «did:XXXXXXXXXX»)
FIELD_UTM_TERM = 869449      # UTM_TERM (keyword из рекламы)

# --- enum-значения поля «Отдел» (FIELD_DEPARTMENT) ---
ENUM_DEPT_OFFLINE = 914379   # «Offline» — все звонковые лиды
ENUM_DEPT_ONLINE = 914381    # «Online»

# --- enum-значения поля «Город» (FIELD_CITY) ---
ENUM_CITY_ALMATY = 860173
ENUM_CITY_SHYMKENT = 860177
ENUM_CITY_DRUGOY = 860179    # «Другой» — дефолт-заглушка, которую чистит cleanup-скрипт
ENUM_CITY_ASTANA = 889947
ENUM_CITY_ATYRAU = 912597
ENUM_CITY_AKTOBE = 912599
ENUM_CITY_ONLINE = 914441

# --- Системные статусы воронки (status_id) ---
STATUS_WON = 142             # «Успешно реализовано»
STATUS_LOST = 143            # «Закрыто и не реализовано»

# --- Пороги sort для расчёта квала/оплаты ---
SORT_QUALIFIED = 80          # КВАЛИФИКАЦИЯ ПРОЙДЕНА и выше
SORT_PAID = 150              # ПРЕДОПЛАТА получена №1 и выше
