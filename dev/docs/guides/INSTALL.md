# نصب دستی Partner Console 1.0.6

برای نصب یا ارتقای مدیریت‌شده در Ubuntu 24.04، نصاب و checksum نسخهٔ هدف را طبق [راهنمای README](../../../README.md#installation) دریافت و بررسی کنید.

این صفحه اجرای دستی `install.sh` از یک درخت سورس تأییدشده و آماده‌کردن تنظیمات، پایگاه و سرویس‌ها را توضیح می‌دهد.

پوشهٔ نصب را خودتان انتخاب می‌کنید. `bash install.sh` اول وارد همان پوشه‌ای می‌شود که اسکریپت داخل آن است، بعد تنظیم، داده، لاگ، cache، فایل موقت و backup را فقط زیر همان پوشه می‌سازد. لازم نیست پوشهٔ جاری قابل‌نوشتن باشد. با جابه‌جا کردن پوشه لازم نیست مسیر ثابتی را در اسکریپت‌ها ویرایش کنید؛ بعد از جابه‌جایی یک بار دیگر `bash install.sh` را اجرا کنید تا محیط Python دوباره ساخته شود. `install.sh` اسکریپت‌های پوسته را 755 می‌کند و `restart.sh` توقف و شروع را با `bash` صدا می‌زند، پس کپی بدون بیت اجرا هم بعد از نصب کار می‌کند.

فایل‌های Next.js در ریشهٔ همین پوشه هستند (`app/`، `components/`، `lib/`، `public/`، `package.json`). نسخهٔ جداگانه‌ای داخل `frontend/` ساخته نشده است.

## چه چیزی نصب پیش‌فرض انجام نمی‌دهد

`install.sh` بستهٔ سیستمی نصب نمی‌کند و فایل سرویس یا پیکربندی وب‌سرور را بیرون از پوشه نمی‌نویسد. Python 3.12 یا جدیدتر، Node.js 22 یا جدیدتر، و npm باید از قبل روی PATH باشند. MySQL 8.4، nginx، TLS و systemd وابستگی میزبان هستند و جدا از فایل‌های برنامه مانده‌اند.

## ترتیب

```bash
bash install.sh
```

برای پایگاه داخل خود پوشه، به‌جای آن:

```bash
bash install.sh --local-database
```

این گزینه فقط وقتی `config/api.env` وجود ندارد فایل SQLite را روی `data/partnerconsole.db` می‌گذارد. SQLite توسعهٔ `backend/data/partner.db` عمداً انتخاب نمی‌شود.

سپس `config/api.env` و `config/web.env` را ویرایش کنید. هر مقداری که با `REPLACE_WITH` شروع شده باید عوض شود. مبدأ وب فقط origin است و `/api` ندارد، چون کلاینت آن را اضافه می‌کند. بعد از تغییر مبدأ وب، `bash install.sh` را دوباره اجرا کنید تا build تازه شود.

`ENVIRONMENT=production` مبدأ CORS را فقط به‌صورت HTTPS دقیق می‌پذیرد. رمز ادمین را در فایل تنظیم ننویسید.

```bash
bash scripts/bootstrap.sh --confirm-database
bash start.sh
bash status.sh
```

bootstrap قبل از هر تغییری نوع پایگاه را بدون رمز چاپ می‌کند و بدون `--confirm-database` هیچ چیز نمی‌نویسد. در Production مهاجرت خودکار هنگام بالا آمدن worker انجام نمی‌شود.

API روی `127.0.0.1:8741` با دو worker و وب روی `127.0.0.1:3741` گوش می‌دهند. ورود ادمین مسیر `/gate/k3p8n2` روی همان وب است. سلامت: `http://127.0.0.1:8741/api/health`. این سلامت اتصال پایگاه را بعد از start دوباره ثابت نمی‌کند.

## MySQL میزبان

اگر پایگاه داخل پوشه کافی نیست، MySQL 8.4 را خودتان نصب کنید و فقط روی `127.0.0.1` گوش بدهید. برنامه روی هر اتصال `READ COMMITTED`، UTC، `utf8mb4` و `utf8mb4_0900_bin` را تنظیم می‌کند. schema باید خالی و از پیش ساخته شده باشد. نمونهٔ ساخت schema در `OPERATIONS.md` است. `install.sh` این کار را انجام نمی‌دهد.

## قالب میزبان، بدون نصب

```bash
bash scripts/render-host-templates.sh
```

خروجی فقط در `release/v1.0.0/generated/` نوشته می‌شود. کپی به مدیر سرویس یا وب‌سرور با اپراتور است و جزو نصب پیش‌فرض نیست. جزئیات در `PORTABLE.md` و `OPERATIONS.md`.

## نصب روی سرور نهایی

این ترتیب روی Ubuntu 24.04 با Python 3.12، Node.js 22 و MySQL 8.4، با کاربر غیر root و MySQL فقط روی `127.0.0.1`، اجرا شده است. سرور آزمایشی `91.107.242.80` نیست: آنجا schema به نام `partnerpanel` از قبل وجود دارد و نباید دوباره استفاده شود. روی سرور نهایی schema را تازه و خالی بسازید.

MySQL را خودتان نصب کنید و 3306، 8741 و 3741 را در فایروال باز نکنید. تا وقتی خودتان nginx و TLS را عمداً منتشر نکرده‌اید، از بیرون فقط SSH باز بماند.

```bash
useradd --system --create-home --home-dir /srv/ppv1 --shell /bin/bash ppv1
install -d -o ppv1 -g ppv1 -m 755 /srv/partnerconsole
```

درخت همین نسخه را داخل `/srv/partnerconsole` بگذارید و مالک آن را `ppv1` کنید. home کاربر را خود پوشهٔ برنامه نگیرید تا فایل سرگردان در home دیده شود.

به‌عنوان root و از socket محلی، یک schema خالی و یک کاربر محدود به همان schema:

```sql
CREATE DATABASE partnerconsole CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
CREATE USER 'partnerconsole'@'127.0.0.1' IDENTIFIED BY 'REPLACE_WITH_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES
  ON partnerconsole.* TO 'partnerconsole'@'127.0.0.1';
```

قبل از اولین `install.sh` این دو فایل را با mode 600 و مالک همان کاربر بسازید. رمز ادمین را در فایل ننویسید. `REPLACE_WITH` و `panel.example.com` نباید در فایل بمانند، وگرنه build انجام نمی‌شود و start رد می‌شود.

`config/api.env`:

```text
ENVIRONMENT=production
DATABASE_URL=mysql+pymysql://partnerconsole:REPLACE_WITH_PASSWORD@127.0.0.1:3306/partnerconsole?charset=utf8mb4
SECRET_KEY=REPLACE_WITH_LONG_RANDOM
ADMIN_JWT_SECRET=REPLACE_WITH_DIFFERENT_LONG_RANDOM
PARTNER_JWT_SECRET=REPLACE_WITH_THIRD_LONG_RANDOM
PANEL_ENCRYPTION_KEY=
ADMIN_USERNAME=admin
ACCESS_TOKEN_EXPIRE_MINUTES=720
CORS_ORIGINS=https://panel.example.com
API_RATE_LIMIT_PER_MINUTE=120
PANEL_PRIVATE_ORIGINS=
```

رمز پایگاه را percent-encode کنید. سه کلید امضا باید جدا، طولانی و غیرمثالی باشند. `CORS_ORIGINS` فقط origin دقیق HTTPS است و path ندارد. `https://panel.example.com` را با مبدأ واقعی مرورگر عوض کنید.

`config/web.env`:

```text
NEXT_PUBLIC_API_BASE=https://panel.example.com
```

کلاینت خودش `/api` را به این مقدار اضافه می‌کند. این مقدار داخل build می‌رود؛ اگر بعداً عوضش کردید یک بار دیگر `bash install.sh` را اجرا کنید.

```bash
runuser -u ppv1 -- bash /srv/partnerconsole/install.sh
runuser -u ppv1 -- bash /srv/partnerconsole/scripts/bootstrap.sh
```

فرمان دوم بدون `--confirm-database` باید غیرصفر برگردد و جدولی نسازد. نام پایگاه چاپ‌شده را بخوانید. اگر همان schema خالی است:

```bash
runuser -u ppv1 -- env ADMIN_PASSWORD='REPLACE_WITH_INITIAL_ADMIN_PASSWORD' \
  bash /srv/partnerconsole/scripts/bootstrap.sh --confirm-database
unset ADMIN_PASSWORD
runuser -u ppv1 -- bash /srv/partnerconsole/start.sh
runuser -u ppv1 -- bash /srv/partnerconsole/status.sh
```

رمز اولیه حداقل ۱۲ نویسه و غیر از `admin12345` است و فقط برای همان فرآیند است. در Production مهاجرت هنگام بالا آمدن worker انجام نمی‌شود. head مورد انتظار `0005_security_state` است.

بررسی از خود سرور:

```bash
curl -fsS http://127.0.0.1:8741/api/health
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3741/
```

سلامت باید `{"ok": true}` و وب 200 باشد. این سلامت بعد از start اتصال پایگاه را دوباره ثابت نمی‌کند. توقف و شروع دوباره: `bash stop.sh`، `bash restart.sh`، `bash status.sh`. دو نصب همزمان هر دو پورت 8741 و 3741 را می‌خواهند؛ دومی را فقط بعد از stop اولی شروع کنید.

`bash scripts/render-host-templates.sh` واحد systemd و nginx را نصب یا enable نمی‌کند. تا وقتی خودتان آن قالب را با مسیر گواهی واقعی کپی نکرده‌اید، برنامه بعد از reboot بالا نمی‌آید. پورت برنامه را به شبکه باز نکنید.

نصب زیر `/root/partnerconsole` با کاربر غیر root بدون باز کردن مجوز `/root` ممکن نیست. این محدودیت را با `chmod` روی `/root` حل نکنید.
