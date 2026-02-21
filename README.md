# Trading Bot (MEXC USDT-M)

Асинхронный торговый бот для MEXC USDT-M (swap) с:
- генерацией сигналов,
- управлением парами и настройками,
- шифрованием API-ключей,
- логированием в SQLite и файлы,
- консольным интерфейсом (`rich` + `questionary`).

## 1) Установка Python

Рекомендуется Python **3.10+** (лучше 3.11/3.12).

Проверка:

```bash
python --version
```

## 2) Создание и активация venv

### Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
```

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

## 3) Установка зависимостей

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## 4) Создание API ключей MEXC (ОБЯЗАТЕЛЬНО Unified Account)

В кабинете MEXC создайте API-ключи для **Unified Account** и включите права:

- `Read`
- `Futures Trading`

> Важно: без режима Unified Account торговля USDT-M может работать некорректно.

При первом запуске бот автоматически создаст файл `master.key` и сможет шифровать API-ключи в БД.

## 5) Запуск

```bash
python main.py
```

## Горячие клавиши

- `m` — открыть меню
- `q` — выйти из бота

## Основные файлы

- `trading_bot.db` — основная база данных SQLite (состояние бота, настройки, ордера, логи).
- `operations.log` — операционный лог (действия бота).
- `bot.log` — общий лог приложения (инициализация, ошибки, runtime-события).
- `master.key` — мастер-ключ для шифрования API-ключей (хранить в безопасности, не коммитить в git).

## Примечания по безопасности

- Никогда не публикуйте `master.key`.
- Не передавайте API-ключи в открытом виде.
- Для реальной торговли рекомендуется сначала протестировать на минимальных объёмах.
