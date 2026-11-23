# اعتبارسنجی MySQL برای Production

**وضعیت فعلی: MYSQL CONDITIONALLY VALIDATED.** همهٔ آزمون‌های MySQL روی سرور واقعی MySQL Community 8.4.13 LTS اجرا شدند و موفق بودند. شرط پذیرش نهایی تطبیق تنظیمات سرور Production با بخش «محدودیت‌ها» است.

## محیط آزمایشی

- سرور: `mysqld 8.4.13` از ZIP رسمی (MD5 تطبیق‌داده‌شده با صفحهٔ دانلود)، بدون سرویس Windows و بدون دسترسی Administrator، با data directory تازه.
- شبکه: فقط `127.0.0.1:3307`؛ پروتکل X خاموش، `local_infile=0`.
- حساب آزمون: `partnerpanel_tester@127.0.0.1` فقط با `ALL ON partnerpanel\_test\_%.*`؛ بدون دسترسی سراسری یا GRANT OPTION.
- درایور: `PyMySQL 1.2.4` در `backend/.venv`.
- مقادیر مؤثر نشست برنامه: InnoDB، `READ-COMMITTED`، `time_zone=+00:00`، `utf8mb4` / `utf8mb4_0900_bin`، `STRICT_TRANS_TABLES`، `innodb_lock_wait_timeout=50`.
- پیش از هر اجرای نوشتنی، port، bind، datadir، `server_uuid`، `CURRENT_USER()`، grantها و خالی‌بودن schema مقصد بررسی شد. داده‌ها همه ساختگی‌اند و هیچ پنل واقعی فراخوانی نشد.

## اجرای آزمون‌ها

از پوشهٔ `backend`، با نشانی schema خالی و مصوب که فقط به همان فرآیند داده می‌شود:

```powershell
$env:PARTNERPANEL_MYSQL_TEST_URL = '<mysql+pymysql://...@127.0.0.1:3307/partnerpanel_test_xxx?charset=utf8mb4>'
$env:PARTNERPANEL_MYSQL_TEST_ALLOW_SCHEMA_WRITES = 'isolated-schema-approved'
.\.venv\Scripts\python.exe -B -m pytest -c pyproject.toml ../dev/tests/backend/test_mysql_integration.py ../dev/tests/backend/test_mysql_test_isolation.py -q -ra -p no:cacheprovider
# با schema خالی دیگر:
.\.venv\Scripts\python.exe -B -m pytest -c pyproject.toml ../dev/tests/backend/test_mysql_release_validation.py -q -ra -p no:cacheprovider
```

`test_mysql_release_validation.py` برای هر آزمون یک schema فرزند `<base>_<n>` می‌سازد و هیچ schema را حذف یا پاک نمی‌کند؛ بنابراین حساب آزمون باید به پیشوند `partnerpanel_test_` محدود باشد. بدون دو متغیر بالا همهٔ آزمون‌های MySQL رد (skip) می‌شوند.

## نتیجه‌ها

| مجموعه | نتیجه |
|---|---|
| `test_mysql_integration.py` (۸ آزمون قبلاً اجرانشده) + ۴ آزمون جداسازی | ۱۲ موفق |
| `test_mysql_release_validation.py` | ۵۶ موفق |
| کل بک‌اند بدون MySQL (SQLite) | ۲۸۵ موفق، ۶۴ رد به‌دلیل نبود متغیر MySQL |
| فرانت‌اند، TypeScript، build | ۱۵ موفق، بدون خطا، موفق |

`test_mysql_release_validation.py` روی MySQL واقعی این موارد را پوشش می‌دهد:

- بازاجرای کامل سناریوهای مالی و بازیابی `test_billing` (۳۰ آزمون)، قفل و دسته‌ای `test_stage35` (۱۴) و HTTP و crash پردازه `test_stage6_http` (۲). معادل MySQL بررسی قفل SQLite با اتصال دوم و `innodb_lock_wait_timeout=1` اثبات می‌کند حین فراخوانی پنل قفل ردیفی روی partners، credits، subscriptions و panel_operations باقی نمی‌ماند (کنترل منفی: قفل نگه‌داشته‌شده با خطای 1205 تشخیص داده شد).
- bootstrap با فرمان واقعی روی schema خالی و اجرای مجدد، حفظ رمز ادمین موجود، چهار bootstrap هم‌زمان (یک ادمین؛ بقیه با قفل نام‌دار متوقف شدند)، رد worker روی schema خالی یا 0004، رد worker و bootstrap حین قفل نگهداری، شروع هم‌زمان دو worker پس از آماده‌سازی.
- شکست میانی DDL در 0005: revision روی 0004 ماند، جدول اول 0005 به‌دلیل commit ضمنی DDL باقی ماند، ادمین ساخته نشد، دادهٔ مالی حفظ شد، worker رد شد و اجرای دوباره هم بدون stamp حدسی شکست خورد.
- تطابق revision با schema واقعی با `compare_metadata`؛ فقط چهار اختلاف شناخته‌شده مجازند (بخش یافته‌ها).
- هشت پردازه روی یک کیف پول: بدون از دست رفتن به‌روزرسانی و بدون موجودی منفی (دقیقاً ۳ برداشت از ۸ پذیرفته شد).
- lock wait timeout (1205)، deadlock (1213؛ تراکنش قربانی کامل rollback شد)، `KILL` اتصال وسط تراکنش (rollback و آزادشدن قفل)، قطع همهٔ اتصال‌های worker پس از اعمال پنل (یک کسر، یک ledger، بدون بازپرداخت).

## backup و restore

تمرین روی schemaهای آزمایشی جدا انجام شد:

1. مبدأ با `python -m app.bootstrap_database` آماده و با دادهٔ ساختگی پر شد: موجودی و دفترکل، سه اشتراک، یک عملیات pending ناشی از پاسخ مبهم پنل و یک مورد attention.
2. هم‌زمان با نوشتن مداوم تراکنش‌های مالی:
   `mysqldump --single-transaction --quick --no-tablespaces --skip-routines --skip-events --triggers --set-gtid-purged=OFF --hex-blob --default-character-set=utf8mb4 <schema>`
   (اعتبار از option file موقت، نه خط فرمان).
3. بازیابی در schema خالی جدا: `mysql --batch <target> < dump.sql`.

نتیجه: ۱۷ تراکنش حین dump commit شد و نسخهٔ بازیابی‌شده همچنان «موجودی = جمع دفترکل تومان» را داشت؛ revision، هویت اشتراک‌ها، pending و attention یکسان بودند. در backup دوم پس از توقف نوشتن، تعداد ردیف و `CHECKSUM TABLE` هر ۱۱ جدول، چهار قید CHECK و دو ایندکس تابعی با مبدأ برابر بود. عملیات pending بازیابی‌شده با همان پنل شبیه‌سازی‌شده یک‌بار تکمیل شد، بدون کسر یا بازپرداخت دوباره.

snapshot وضعیت بیرونی پنل را برنمی‌گرداند. پس از restore و پیش از بازگشایی نوشتن، عملیات pending و attention را با پنل تطبیق دهید.

## یافته‌ها

- **خطای CHECK در MySQL از نوع `OperationalError` است (کم).** PyMySQL کد 3819 را به `IntegrityError` نگاشت نمی‌کند. قید موجودی منفی درست عمل می‌کند و کد برنامه به کلاس این خطا وابسته نیست (کسرها پیش از تغییر بررسی می‌شوند و مسیرهای جبران هر `Exception` را می‌گیرند). فقط انتظار آزمون اصلاح شد.
- **ایندکس یکتای تکراری روی `username` (کم).** 0001 روی `admin_users` و `partners` هم `UniqueConstraint` و هم `ix_*_username` یکتا ساخته است. اثر فقط هزینهٔ نوشتن و فضای اضافه است؛ اصلاح نیازمند migration جدید است و در این مرحله انجام نشد.
- **commit ضمنی DDL (رفتار شناخته‌شدهٔ MySQL).** شکست میانی migration بخشی از تغییرات را باقی می‌گذارد. روند [راهنمای bootstrap](production-bootstrap.md) (توقف workerها، عدم تکرار کورکورانه، restore از backup) با رفتار مشاهده‌شده سازگار است.

## محدودیت‌ها

- سرور آزمایشی روی Windows است و `lower_case_table_names=1` دارد؛ پیش‌فرض Linux مقدار 0 است. نام جدول‌ها همه حروف کوچک‌اند، اما این تفاوت باید در Production بررسی شود.
- تنظیمات Production (`innodb_lock_wait_timeout`، `sql_mode` سراسری، `binlog_format`، replication، اندازهٔ pool) از سرور واقعی استعلام نشده‌اند.
- پنل فقط با شبیه‌ساز محلی آزموده شد؛ رفتار V2Board واقعی و شبکهٔ Production آزموده نشده‌اند.
