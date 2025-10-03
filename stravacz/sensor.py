from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

_LOGGER = logging.getLogger(__name__)

# -------------------- Config keys --------------------
CONF_NAME = "name"
CONF_DAYS = "days"
CONF_LOGIN_CISLO = "login_cislo"
CONF_LOGIN_JMENO = "login_jmeno"
CONF_LOGIN_HESLO = "login_heslo"
CONF_ENVIRONMENT = "environment"
CONF_LANG = "lang"
CONF_OBJEDNAVKY_CISLO = "objednavky_cislo"
CONF_KONTO = "konto"
CONF_S5URL = "s5url"

# -------------------- Defaults --------------------
DEFAULT_NAME = "Školní jídelna – Strava.cz"
DEFAULT_DAYS = 7
DEFAULT_ENV = "W"
DEFAULT_LANG = "EN"
DEFAULT_SCAN_SECONDS = 3 * 60 * 60  # 3 h

# -------------------- HTTP --------------------
LOGIN_URL = "https://app.strava.cz/api/login"
ORDERS_URL = "https://app.strava.cz/api/objednavky"
BASE_HEADERS = {
    "User-Agent": "HomeAssistant-StravaCZ/1.0 (+https://home-assistant.io)",
    "Referer": "https://app.strava.cz/en/prihlasit-se?jidelna",
    "Origin": "https://app.strava.cz",
    "Content-Type": "text/plain;charset=UTF-8",
}

# -------------------- Schema --------------------
PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_DAYS, default=DEFAULT_DAYS): vol.All(
            int, vol.Range(min=1, max=31)
        ),
        vol.Required(CONF_LOGIN_CISLO): cv.string,
        vol.Required(CONF_LOGIN_JMENO): cv.string,
        vol.Required(CONF_LOGIN_HESLO): cv.string,
        vol.Optional(CONF_ENVIRONMENT, default=DEFAULT_ENV): cv.string,
        vol.Optional(CONF_LANG, default=DEFAULT_LANG): cv.string,
        vol.Required(CONF_OBJEDNAVKY_CISLO): cv.string,
        vol.Optional(CONF_KONTO, default=0): vol.Coerce(int),
        vol.Required(CONF_S5URL): cv.url,
    }
)

# ======================================================
# ===============  Utils (pomocné funkce)  =============
# ======================================================

DATE_MS_RE = re.compile(r"/Date\((\d+)\)/")  # /Date(1696291200000)/

GENERIC_NAMES_FULL = {"polévka", "oběd", "svačina", "přesnídávka"}
GENERIC_NAMES_ASCII = {"polevka", "obed", "svacina", "presnidavka"}

def _redact(login: dict[str, Any]) -> dict[str, Any]:
    safe = dict(login)
    if "heslo" in safe:
        safe["heslo"] = "***"
    return safe

def _parse_date_any(x: Any) -> Optional[date]:
    """Zkusí /Date(ms)/, ISO, dd.mm.yyyy."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        try:
            return datetime.fromtimestamp(float(x), tz=timezone.utc).date()
        except Exception:
            return None
    if isinstance(x, str):
        m = DATE_MS_RE.match(x)
        if m:
            try:
                ms = int(m.group(1))
                return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date()
            except Exception:
                return None
        try:
            return datetime.fromisoformat(x.replace("Z", "+00:00")).date()
        except Exception:
            pass
        try:
            s = x.strip().replace(" ", "")
            return datetime.strptime(s, "%d.%m.%Y").date()
        except Exception:
            pass
    return None

def _is_generic_name(name: str) -> bool:
    n = (name or "").strip().lower()
    return n in GENERIC_NAMES_FULL or n in GENERIC_NAMES_ASCII

def _allergen_codes(it: dict[str, Any]) -> list[str]:
    """Z pairů v 'alergeny' vybere seznam kódů. Fallback parsuje 'alergeny_text'."""
    out: list[str] = []
    arr = it.get("alergeny")
    if isinstance(arr, list):
        for pair in arr:
            if isinstance(pair, list) and pair:
                code = str(pair[0])
                if code.isdigit() and len(code) == 1:
                    code = f"0{code}"
                out.append(code)
    if not out:
        txt = (it.get("alergeny_text") or "").strip("| ")
        for part in txt.split("|"):
            part = part.strip()
            if not part:
                continue
            code = part.split(" ")[0]
            if code.isdigit() and len(code) == 1:
                code = f"0{code}"
            out.append(code)
    # unikátní, stabilní pořadí
    seen = set()
    dedup = []
    for c in out:
        if c not in seen:
            seen.add(c)
            dedup.append(c)
    return dedup

def _mk_dish(name: str, allergens: list[str] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {"name": name}
    if allergens:
        d["allergens"] = allergens
    return d

def _prune(x: Any) -> Any:
    """Rekurzivně smaže None, prázdné stringy a prázdné dict/list."""
    if isinstance(x, dict):
        new = {}
        for k, v in x.items():
            pv = _prune(v)
            if pv is None:
                continue
            if isinstance(pv, (dict, list)) and not pv:
                continue
            if isinstance(pv, str) and pv.strip() == "":
                continue
            new[k] = pv
        return new
    if isinstance(x, list):
        new_list = []
        for v in (_prune(v) for v in x):
            if v is None:
                continue
            if isinstance(v, (dict, list)) and not v:
                continue
            if isinstance(v, str) and v.strip() == "":
                continue
            new_list.append(v)
        return new_list
    return x

# ======================================================
# ===============  Normalizace objednávek  =============
# ======================================================

def _normalize_day(items: List[dict[str, Any]]) -> dict[str, Any]:
    """
    Výstup jednoho dne:
      { lunch: {soup?, main?}, morning_snack?, afternoon_snack? }
    - svačiny jsou VOLITELNÉ
    - placeholder názvy („Polévka/Oběd/Svačina/Přesnídávka“) ignorujeme
    - kódy alergenů bereme z `alergeny`/`alergeny_text`
    - prázdná pole zahodíme
    """
    day_obj: dict[str, Any] = {}
    lunch: dict[str, Any] = {}

    def maybe_set(target: dict, key: str, dish: dict) -> None:
        if key not in target:
            target[key] = dish

    for it in items:
        druh = str(it.get("druh_popis", "")).strip().lower()
        nazev = str(it.get("nazev", "")).strip()
        if not nazev or _is_generic_name(nazev):
            continue

        allergens = _allergen_codes(it)

        # Primární mapování podle 'druh_popis'
        if "polév" in druh or "polev" in druh:
            maybe_set(lunch, "soup", _mk_dish(nazev, allergens))
            continue
        if "oběd" in druh or "obed" in druh:
            maybe_set(lunch, "main", _mk_dish(nazev, allergens))
            continue
        if "přesníd" in druh or "presnid" in druh:
            if "morning_snack" not in day_obj:
                day_obj["morning_snack"] = _mk_dish(nazev, allergens)
            continue
        if "svačin" in druh or "svacin" in druh:
            if "afternoon_snack" not in day_obj:
                day_obj["afternoon_snack"] = _mk_dish(nazev, allergens)
            continue

        # Fallback: heuristika z názvu
        low = nazev.lower()
        if "polév" in low or "polev" in low:
            maybe_set(lunch, "soup", _mk_dish(nazev, allergens))
        elif any(w in low for w in ("svačina", "svacina")):
            if "afternoon_snack" not in day_obj:
                day_obj["afternoon_snack"] = _mk_dish(nazev, allergens)
        else:
            maybe_set(lunch, "main", _mk_dish(nazev, allergens))

    if lunch:
        day_obj["lunch"] = lunch

    # rekurzivní odmazání prázdných polí
    return _prune(day_obj)

def _orders_to_menu(orders_json: Any, keep_days: int) -> dict[str, dict[str, Any]]:
    """
    /api/objednavky → { "YYYY-MM-DD": { lunch:{...}, morning_snack?, afternoon_snack? } }
    - slučuje všechny 'tableX'
    - vypouští prázdné dny po normalizaci/pruningu
    - ořízne na dnešek…(dnes + keep_days)
    """
    by_day: Dict[date, List[dict[str, Any]]] = {}

    if isinstance(orders_json, dict):
        iterables = orders_json.values()
    elif isinstance(orders_json, list):
        iterables = [orders_json]
    else:
        _LOGGER.warning("Unknown orders JSON type: %s", type(orders_json))
        iterables = []

    for lst in iterables:
        if not isinstance(lst, list):
            continue
        for it in lst:
            d = _parse_date_any(it.get("datum")) or _parse_date_any(it.get("den") or it.get("date"))
            if not d:
                continue
            by_day.setdefault(d, []).append(it)

    today = date.today()
    last = today + timedelta(days=keep_days)
    menu: Dict[str, dict[str, Any]] = {}
    for d in sorted(k for k in by_day.keys() if today <= k <= last):
        normalized = _normalize_day(by_day[d])
        if normalized:
            menu[d.isoformat()] = normalized

    return menu

def _select_today(menu: Dict[str, dict[str, Any]]) -> Tuple[Optional[str], dict[str, Any]]:
    if not menu:
        return None, {}
    today = date.today().isoformat()
    if today in menu:
        return today, menu[today] or {}
    # jinak první dostupný den (nejbližší budoucí)
    key = sorted(menu.keys())[0]
    return key, menu.get(key) or {}

# ======================================================
# ===================  API klient  ====================
# ======================================================

class StravaClient:
    def __init__(self, hass: HomeAssistant, login: dict[str, Any], orders: dict[str, Any]) -> None:
        self._hass = hass
        self._login = login
        self._orders = orders

    async def login(self) -> str:
        """Vrátí SID po úspěšném loginu."""
        session = async_get_clientsession(self._hass)
        payload = {
            "cislo": self._login["cislo"],
            "jmeno": self._login["jmeno"],
            "heslo": self._login["heslo"],
            "zustatPrihlasen": False,
            "environment": self._login.get("environment", DEFAULT_ENV),
            "lang": self._login.get("lang", DEFAULT_LANG),
        }
        data = json.dumps(payload, separators=(",", ":"))
        _LOGGER.debug("POST %s login=%s", LOGIN_URL, _redact(self._login))

        async with session.post(LOGIN_URL, data=data, headers=BASE_HEADERS, timeout=20) as resp:
            text = await resp.text()
            if resp.status != 200:
                _LOGGER.error("Login failed (%s): %s", resp.status, text[:500])
                raise RuntimeError(f"Login failed: HTTP {resp.status}")
            try:
                j = json.loads(text)
            except Exception as exc:
                _LOGGER.error("Login returned non-JSON: %s", text[:500])
                raise exc
            sid = j.get("sid")
            if not sid:
                _LOGGER.error("Login JSON has no 'sid': %s", j)
                raise RuntimeError("Login JSON has no 'sid'")
            _LOGGER.info("Login OK for user=%s", str(j.get("uzivatel") or "???"))
            return sid

    async def fetch_orders(self, sid: str) -> Any:
        """Vrátí raw JSON z /api/objednavky."""
        session = async_get_clientsession(self._hass)
        payload = {
            "cislo": self._orders["cislo"],
            "ignoreCert": False,
            "konto": self._orders.get("konto", 0),
            "lang": self._login.get("lang", DEFAULT_LANG),
            "podminka": "",
            "s5url": self._orders["s5url"],
            "sid": sid,
        }
        data = json.dumps(payload, separators=(",", ":"))
        safe_payload = dict(payload); safe_payload["sid"] = "***"
        _LOGGER.debug("POST %s orders=%s", ORDERS_URL, safe_payload)

        async with session.post(ORDERS_URL, data=data, headers=BASE_HEADERS, timeout=20) as resp:
            text = await resp.text()
            if resp.status != 200:
                _LOGGER.error("Orders failed (%s): %s", resp.status, text[:500])
                raise RuntimeError(f"Orders failed: HTTP {resp.status}")
            try:
                return json.loads(text)
            except Exception as exc:
                _LOGGER.error("Orders returned non-JSON: %s", text[:500])
                raise exc

# ======================================================
# =================  Coordinator  ======================
# ======================================================

class StravaCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    def __init__(self, hass: HomeAssistant, name: str, login: dict[str, Any], orders: dict[str, Any], days: int):
        super().__init__(
            hass,
            _LOGGER,
            name=f"StravaCZ {name}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_SECONDS),
        )
        self._client = StravaClient(hass, login, orders)
        self._days = days
        self._name = name

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        _LOGGER.debug("[%s] Starting update", self._name)
        sid = await self._client.login()
        raw = await self._client.fetch_orders(sid)
        menu = _orders_to_menu(raw, self._days)
        _LOGGER.info("[%s] Got menu days=%s", self._name, len(menu))
        return menu

# ======================================================
# =================  Platform setup  ===================
# ======================================================

async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    try:
        name = config.get(CONF_NAME, DEFAULT_NAME)
        days = config.get(CONF_DAYS, DEFAULT_DAYS)

        login = {
            "cislo": config[CONF_LOGIN_CISLO],
            "jmeno": config[CONF_LOGIN_JMENO],
            "heslo": config[CONF_LOGIN_HESLO],
            "environment": config.get(CONF_ENVIRONMENT, DEFAULT_ENV),
            "lang": config.get(CONF_LANG, DEFAULT_LANG),
        }
        orders = {
            "cislo": config[CONF_OBJEDNAVKY_CISLO],
            "konto": config.get(CONF_KONTO, 0),
            "s5url": config[CONF_S5URL],
        }

        _LOGGER.info("Setting up '%s' (login=%s, orders=%s, days=%s)", name, _redact(login), orders, days)

        coordinator = StravaCoordinator(hass, name, login, orders, days)
        await coordinator.async_refresh()  # první fetch hned
        if not coordinator.last_update_success:
            _LOGGER.warning("Initial update failed for '%s'", name)

        # Vytvoř 4 entity (přesnídávka, polévka, hlavní chod, svačina)
        entities: list[StravaCzDishSensor] = [
            StravaCzDishSensor(name, coordinator, login, orders, part="morning_snack"),
            StravaCzDishSensor(name, coordinator, login, orders, part="lunch_soup"),
            StravaCzDishSensor(name, coordinator, login, orders, part="lunch_main"),
            StravaCzDishSensor(name, coordinator, login, orders, part="afternoon_snack"),
        ]
        add_entities(entities, True)
    except Exception:
        _LOGGER.exception("Failed to set up Strava.cz sensor")
        raise

# ======================================================
# ====================  Entity  ========================
# ======================================================

_PART_META = {
    "morning_snack": ("Přesnídávka", "mdi:bread-slice"),
    "lunch_soup": ("Oběd – Polévka", "mdi:pot-steam"),
    "lunch_main": ("Oběd – Hlavní chod", "mdi:food"),
    "afternoon_snack": ("Svačina", "mdi:food-apple"),
}

class StravaCzDishSensor(CoordinatorEntity[StravaCoordinator], SensorEntity):
    """Jedna část denní nabídky jako samostatný senzor."""

    def __init__(self, name: str, coordinator: StravaCoordinator, login: dict[str, Any], orders: dict[str, Any], part: str) -> None:
        super().__init__(coordinator)
        if part not in _PART_META:
            raise ValueError(f"Unknown part '{part}'")

        self._base_name = name
        self._login = login
        self._orders = orders
        self._part = part

        label, icon = _PART_META[part]
        self._attr_name = f"{name} {label}"
        self._attr_icon = icon
        self._attr_unique_id = f"stravacz_{login['cislo']}_{login['jmeno']}_{part}"

    # ---- helpers ----
    def _get_today_piece(self) -> Tuple[Optional[str], Optional[dict]]:
        """Vrátí (date_key, dish_dict) pro danou část dne."""
        menu = self.coordinator.data or {}
        key, day = _select_today(menu)
        if not key or not day:
            return None, None

        if self._part == "morning_snack":
            return key, day.get("morning_snack")
        if self._part == "afternoon_snack":
            return key, day.get("afternoon_snack")

        lunch = day.get("lunch") or {}
        if self._part == "lunch_soup":
            return key, lunch.get("soup")
        if self._part == "lunch_main":
            return key, lunch.get("main")

        return key, None

    # ---- HA props ----
    @property
    def native_value(self) -> Optional[str]:
        """Název jídla (nebo None, pokud není k dispozici)."""
        _, dish = self._get_today_piece()
        if not dish:
            return None
        return dish.get("name")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        date_key, dish = self._get_today_piece()
        attrs = {
            "date": date_key,
            "allergens": (dish or {}).get("allergens"),
            "account": {
                "login": _redact(self._login),
                "orders": self._orders,
            },
            "last_update_success": self.coordinator.last_update_success,
        }
        return _prune(attrs)
