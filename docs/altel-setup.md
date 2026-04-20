# Altel / Tele2 SIP trunks — setup guide

Инструкция по подключению 6 Altel SIP-номеров к FreePBX и KuroTrack.

## Шаг 1. Сообщить провайдеру свой IP

Ваш публичный IP (`45.136.56.159`) **должен быть добавлен в whitelist** у Altel.
Без этого регистрация не пройдёт. Напишите менеджеру.

## Шаг 2. Открыть порты на firewall сервера

Altel использует RTP-порты `10002-59999` и стандартный SIP-порт `5060`.

```bash
# Разрешить SIP signaling (UDP 5060)
iptables -A INPUT -p udp --dport 5060 -j ACCEPT

# Разрешить RTP media
iptables -A INPUT -p udp --dport 10002:59999 -j ACCEPT

# Разрешить входящий трафик от серверов Altel
for ip in 217.76.70.8 217.76.70.108 185.57.74.8 185.57.74.108 \
          212.96.77.188 212.96.77.238 212.96.68.158 212.96.68.168; do
  iptables -A INPUT -s $ip -j ACCEPT
done

# Сохранить правила
iptables-save > /etc/iptables/rules.v4
```

Если используется FreePBX Firewall (Sysadmin → Firewall), добавьте IP Altel в **Trusted zone**.

## Шаг 3. Сгенерировать pjsip-конфиг

На машине разработки:

```bash
cd /path/to/kuro/backend/asterisk
cp trunks.csv.example trunks.csv
# Отредактировать trunks.csv — вставить реальные пароли
nano trunks.csv

# Сгенерировать pjsip_custom.conf
cd /path/to/kuro/backend
python -m asterisk.scripts.generate_pjsip
```

## Шаг 4. Установить конфиг на FreePBX

```bash
# Скопировать сгенерированный конфиг на сервер FreePBX
scp backend/asterisk/pjsip_custom.conf root@45.136.56.159:/etc/asterisk/

# Скопировать dialplan
scp backend/asterisk/dialplan/extensions_custom.conf \
    root@45.136.56.159:/etc/asterisk/

# Скопировать AGI-скрипт
scp backend/asterisk/agi/call_tracking.py \
    root@45.136.56.159:/var/lib/asterisk/agi-bin/

# На FreePBX сервере:
ssh root@45.136.56.159
chmod +x /var/lib/asterisk/agi-bin/call_tracking.py
chown asterisk:asterisk /var/lib/asterisk/agi-bin/call_tracking.py

# Перезагрузить PJSIP и dialplan
asterisk -rx "pjsip reload"
asterisk -rx "dialplan reload"

# Проверить регистрацию (должно быть 6 Registered)
asterisk -rx "pjsip show registrations"
```

## Шаг 5. Настроить Inbound Routes в FreePBX GUI

Для каждого из 6 DID (7004982670/675/680/682/687/690):

**Connectivity → Inbound Routes → Add Inbound Route**

- **Description**: `KuroTrack — 7004982XXX`
- **DID Number**: `7004982XXX`
- **Set Destination**: **Custom Destinations → kurotrack-inbound,${DID},1**
  *(если нет Custom Destinations — создать: Admin → Custom Destinations)*

## Шаг 6. Добавить номера в KuroTrack

```bash
# Скрипт автоматически распределит номера по каналам
docker compose exec api python -m app.scripts.add_altel_numbers
```

Распределение по умолчанию:
- 3 номера — **динамический пул** (для сайта)
- 3 номера — **статические** (2GIS Алматы, Астана, Шымкент)

Остальные города (Атырау, Актобе) и Facebook — добавите когда закупите ещё номера.

## Шаг 7. Проверить

1. Откройте дашборд KuroTrack
2. **Numbers** — должны быть 6 номеров (3 dynamic + 3 static)
3. Позвоните на любой из номеров
4. В **Calls** появится запись со статусом ANSWERED и источником
