"""Когда слать тревогу. Чистая логика без сети и базы - тестируется напрямую.

Правила против ложных и лишних тревог (идеи Uptime Kuma и Alertmanager):

  1. Подтверждение. Одна неудачная проверка - ещё не падение: TCP через VPN
     и SOCKS иногда теряет коннект. Тревога - только после down_confirm
     кругов подряд. То же для туннеля точки: один сбой рукопожатия SOCKS -
     не «объект недоступен».
  2. Объект целиком. Лёг туннель точки или не отвечает большая часть её
     устройств - одно сообщение про объект, а не двадцать про камеры.
     Выход из этого состояния - с запасом (порог ниже порога входа), иначе
     объект около порога мигал бы «недоступен / на связи» каждый круг.
  3. Окно обслуживания («не тревожить»). Изменения в окне учитываются молча.
     Если окно кончилось, а устройство или объект всё ещё лежит - приходит
     одно сообщение «всё ещё не отвечает»: камеру обслужили, она не
     поднялась - об этом надо узнать.
  4. О подъёме сообщаем, только если сообщали о падении: «снова на связи» по
     падению, о котором не слышали, только путает.

Что уже сообщено - в памяти. После перезапуска берётся из последней записи
истории (baseline), прочитанной ДО записи текущего круга: лежавшее до
рестарта не тревожит повторно, упавшее во время рестарта - тревожит.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable

# baseline(device_id) -> (последний записанный статус, когда записан, ISO)
Baseline = Callable[[str], tuple[str | None, str | None]]


@dataclass(frozen=True)
class AlertRules:
    down_confirm: int = 3           # кругов подряд без ответа до тревоги
    site_outage_ratio: float = 0.7   # доля упавших, с которой «лежит объект»
    site_outage_min: int = 3        # меньше устройств - объект целиком не считаем
    repeat_hours: float = 0         # повтор тревоги по долгому падению; 0 - нет
    exit_margin: float = 0.2        # запас при выходе из «объект недоступен»


@dataclass(frozen=True)
class Observation:
    device_id: str
    name: str
    site: str
    host: str
    status: str  # up | down


@dataclass
class _DeviceState:
    alerted: str                    # какое состояние уже учтено: up | down
    notified: bool = False          # сообщили ли о текущем падении
    streak: int = 0                 # кругов подряд в down
    down_since: datetime | None = None
    last_sent: datetime | None = None


@dataclass
class _SiteState:
    since: datetime
    notified: bool = False


@dataclass
class Message:
    """Одна строка оповещения; группируются по точке при отправке."""
    site: str
    text: str
    kind: str  # down | up | still | repeat | site_down | site_up


def _fmt_duration(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} д {hours} ч" if hours else f"{days} д"


def _parse(at: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(at) if at else None
    except ValueError:
        return None


class AlertEngine:
    def __init__(self, rules: AlertRules, baseline: Baseline, tz=None):
        self.rules = rules
        self._baseline = baseline
        self._tz = tz
        self._devices: dict[str, _DeviceState] = {}
        self._sites: dict[str, _SiteState] = {}
        self._tunnel_streak: dict[str, int] = {}
        self._today = None

    def _clock(self, moment: datetime) -> str:
        local = moment.astimezone(self._tz) if self._tz else moment
        return local.strftime("%d.%m %H:%M") if (local.date() != self._today) else local.strftime("%H:%M")

    def _state(self, device_id: str, now: datetime) -> _DeviceState:
        state = self._devices.get(device_id)
        if state is None:
            status, at = self._baseline(device_id)
            if status == "down":
                # О падении до рестарта уже сообщали (или оно было до
                # появления оповещений) - повторно не шлём. Повтор считаем от
                # момента рестарта, длительность простоя - от записи в истории.
                state = _DeviceState(alerted="down", notified=True,
                                     down_since=_parse(at), last_sent=now)
            else:
                state = _DeviceState(alerted="up")
            self._devices[device_id] = state
        return state

    def evaluate(self, observations: Iterable[Observation], tunnels: dict[str, dict],
                 muted: Callable[[str, str], bool], now: datetime) -> list[Message]:
        rules = self.rules
        local_now = now.astimezone(self._tz) if self._tz else now
        self._today = local_now.date()
        messages: list[Message] = []
        by_site: dict[str, list[Observation]] = {}

        for obs in observations:
            by_site.setdefault(obs.site, []).append(obs)
            state = self._state(obs.device_id, now)
            if obs.status == "down":
                state.streak += 1
                if state.down_since is None:
                    state.down_since = now
            else:
                state.streak = 0

        # Удалённые устройства и точки забываем: иначе точка, имя которой
        # потом используют снова, получила бы ложное «снова на связи».
        seen = {obs.device_id for items in by_site.values() for obs in items}
        for gone in set(self._devices) - seen:
            del self._devices[gone]
        for gone in set(self._sites) - set(by_site):
            del self._sites[gone]
        for gone in set(self._tunnel_streak) - set(by_site):
            del self._tunnel_streak[gone]

        for site, items in by_site.items():
            states = [(obs, self._devices[obs.device_id]) for obs in items]
            site_quiet = muted("site", site)

            tunnel = tunnels.get(site) or {}
            tunnel_down = tunnel.get("status") == "down"
            self._tunnel_streak[site] = self._tunnel_streak.get(site, 0) + 1 if tunnel_down else 0

            in_outage = site in self._sites
            confirmed = [obs for obs, st in states
                         if obs.status == "down"
                         and (st.alerted == "down" or st.streak >= rules.down_confirm)]
            threshold = rules.site_outage_ratio - (rules.exit_margin if in_outage else 0)
            ratio_out = (len(items) >= rules.site_outage_min and bool(confirmed)
                         and len(confirmed) / len(items) >= threshold)
            tunnel_out = tunnel_down and (in_outage or
                                          self._tunnel_streak[site] >= rules.down_confirm)

            if tunnel_out or ratio_out:
                outage = self._sites.setdefault(site, _SiteState(since=now))
                if not outage.notified and not site_quiet:
                    reason = (f"туннель: {tunnel.get('detail') or 'нет связи'}" if tunnel_out
                              else f"не отвечают {len(confirmed)} из {len(items)}")
                    messages.append(Message(site, f"🔴 Объект недоступен — {reason}", "site_down"))
                    outage.notified = True
                # Пока объект лежит, по его устройствам молчим, но помечаем их
                # упавшими: после восстановления объекта оставшиеся лежать
                # перечислятся, а их подъём потом сообщится.
                for obs, st in states:
                    if obs.status == "down" and st.alerted != "down":
                        st.alerted = "down"
                        st.notified = False
                continue

            if in_outage:
                outage = self._sites.pop(site)
                still = [(obs, st) for obs, st in states if obs.status == "down"]
                announce = outage.notified and not site_quiet
                if announce:
                    text = f"🟢 Объект снова на связи, простой {_fmt_duration(now - outage.since)}"
                    if still:
                        text += f". Не отвечают: {', '.join(sorted(o.name for o, _ in still))}"
                    messages.append(Message(site, text, "site_up"))
                for obs, st in states:
                    if obs.status == "up":
                        st.alerted, st.notified, st.down_since = "up", False, None
                    else:
                        # Перечисленные в «Не отвечают» считаются сообщёнными;
                        # повтор - отсчитываем от этого сообщения.
                        st.alerted = "down"
                        st.notified = announce
                        st.last_sent = now
                continue

            for obs, st in states:
                quiet = site_quiet or muted("device", obs.device_id)
                if obs.status == "down":
                    since = self._clock(st.down_since or now)
                    if st.alerted != "down":
                        if st.streak >= rules.down_confirm:
                            st.alerted = "down"
                            st.notified = not quiet
                            st.last_sent = now
                            if not quiet:
                                messages.append(Message(
                                    site, f"🔴 {obs.name} ({obs.host}) не отвечает с {since}", "down"))
                    elif not st.notified:
                        # Падение было учтено молча (окно обслуживания, объект
                        # целиком, оповещения ещё не настроены) - окна больше
                        # нет, а устройство лежит: сообщаем один раз.
                        if not quiet:
                            st.notified = True
                            st.last_sent = now
                            messages.append(Message(
                                site, f"🔴 {obs.name} ({obs.host}) всё ещё не отвечает, с {since}",
                                "still"))
                    elif (rules.repeat_hours and not quiet and st.last_sent
                          and now - st.last_sent >= timedelta(hours=rules.repeat_hours)):
                        st.last_sent = now
                        lasting = f", {_fmt_duration(now - st.down_since)}" if st.down_since else ""
                        messages.append(Message(
                            site, f"⏰ {obs.name} ({obs.host}) всё ещё не отвечает{lasting}", "repeat"))
                elif st.alerted == "down":
                    downtime = _fmt_duration(now - st.down_since) if st.down_since else ""
                    was_notified = st.notified
                    st.alerted, st.notified, st.down_since = "up", False, None
                    if was_notified and not quiet:
                        tail = f", простой {downtime}" if downtime else ""
                        messages.append(Message(
                            site, f"🟢 {obs.name} ({obs.host}) снова на связи{tail}", "up"))
                else:
                    st.down_since = None   # мигнуло и поднялось до подтверждения
        return messages


def render(messages: list[Message], limit: int = 3900) -> list[str]:
    """Сообщения круга - в тексты для отправки: по точкам, не длиннее limit
    (у Telegram предел 4096 символов). Длинный блок делится по строкам, строки
    не теряются и не режутся посередине."""
    grouped: dict[str, list[str]] = {}
    for message in messages:
        grouped.setdefault(message.site, []).append(message.text[:limit - 200])
    out: list[str] = []
    current = ""

    def push(piece: str) -> None:
        nonlocal current
        if current and len(current) + len(piece) > limit:
            out.append(current)
            current = piece.lstrip("\n")
        else:
            current += piece

    for site, lines in grouped.items():
        header = f"\n\n{site}" if current else site
        push(header)
        for line in lines:
            before = len(out)
            push(f"\n{line}")
            if len(out) != before:      # строка ушла в новое сообщение -
                current = f"{site} (продолжение)\n{current}"   # повторяем заголовок
    if current:
        out.append(current)
    return out


@dataclass
class Mutes:
    """Окна «не тревожить»: ключ "site:<имя>" или "device:<id>" -> до когда."""
    until: dict[str, datetime] = field(default_factory=dict)

    def active(self, kind: str, target: str, now: datetime) -> bool:
        end = self.until.get(f"{kind}:{target}")
        return end is not None and end > now
