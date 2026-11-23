# اجرا، نگهداری و بازگشت

منطق مالی، journal، pending، attention، lease و recovery عوض نشده است. worker در `ENVIRONMENT=production` مهاجرت نمی‌کند. قبل از bootstrap هر دو فرآیند باید ایستاده باشند.

Head مهاجرت `0005_security_state` است:

`0001_initial` → `0002_panel_operations` → `0003_operation_lease` → `0004_subscription_identity` → `0005_security_state`

Downgrade نسخهٔ `0005` جدول ابطال توکن و شمارنده را حذف می‌کند و روش بازگشت نیست. `alembic stamp` و ویرایش دستی revision ممنوع است مگر طرح جداگانه‌ای روی نسخهٔ آزمایشی تأیید شده باشد.

## فرمان‌ها

همه از ریشهٔ پوشهٔ نصب اجرا می‌شوند:

```bash
bash start.sh
bash stop.sh
bash restart.sh
bash status.sh
bash scripts/bootstrap.sh --confirm-database
bash scripts/reset-admin-password.sh --confirm-database USERNAME
bash scripts/backup.sh
```

reset رمز را مخفی می‌پرسد، کاربر جدید نمی‌سازد و مهاجرت نمی‌کند. تغییر رمز توکن‌های قبلی را باطل نمی‌کند. برای بستن نشست ادمین `ADMIN_JWT_SECRET` را در `config/api.env` عوض کنید و restart کنید. برای همکار `PARTNER_JWT_SECRET` را عوض کنید. `SECRET_KEY` و `PANEL_ENCRYPTION_KEY` را برای بستن نشست نچرخانید.

## پایگاه MySQL

رمز را percent-encode کنید و در گزارش ننویسید.

```sql
CREATE DATABASE partnerconsole CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
CREATE USER 'partnerconsole'@'127.0.0.1' IDENTIFIED BY 'REPLACE_WITH_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, ALTER, INDEX, REFERENCES
  ON partnerconsole.* TO 'partnerconsole'@'127.0.0.1';
```

`DATABASE_URL` را در `config/api.env` بگذارید. bootstrap نام میزبان و نام schema را بدون رمز نشان می‌دهد. اگر آن خط پایگاه مورد نظر شما نیست، فرمان را قطع کنید.

## قالب اختیاری مدیر سرویس

نصب پیش‌فرض با `start.sh` فرآیندها را در session جدا بالا می‌آورد و pid را در `runtime/pids/` می‌گذارد. برای قالب میزبان:

```bash
bash scripts/render-host-templates.sh
```

فایل‌های `release/v1.0.0/generated/` مسیر همین پوشه را دارند. این اسکریپت آن‌ها را در مدیر سرویس نصب نمی‌کند. اگر خودتان کپی کردید، همزمان `start.sh` را هم اجرا نکنید. هر دو یک پورت را می‌خواهند.

قالب، temp خصوصی سیستم نمی‌سازد و متغیر temp را به `runtime/tmp` می‌دهد. `ProtectHome` خاموش است تا پوشه بتواند داخل home باشد. لاگ واحد به `logs/api.log` و `logs/web.log` می‌رود.

نمونهٔ nginx در `release/v1.0.0/nginx/partnerconsole.conf` است. نام میزبان و مسیر گواهی را خودتان می‌گذارید. گواهی فایل برنامه نیست. API پشت وب‌سرور می‌ماند و `X-Forwarded-For` باید جایگزین شود، نه به مقدار کلاینت چسبانده شود. پورت‌های 8741، 3741 و 3306 را به شبکه باز نکنید.

## بازگشت

1. `bash stop.sh`
2. اگر مهاجرت نسخهٔ جدید اعمال شده، کد قدیمی را روی schema جدید شروع نکنید.
3. backup را در schema یا فایل جدا برگردانید و مانده، اعتبار محصول، `v2_user_id`، journal، pending، attention و lease را مقایسه کنید.
4. فقط بعد از این مقایسه تنظیم را به مقصد بازیابی‌شده ببرید و همان نسخه‌ای را شروع کنید که revision آن با پایگاه یکی است.
5. بازیابی پایگاه، اثرهای بعدی V2Board را برنمی‌گرداند.

در MySQL یک DDL ممکن است وسط شکست commit شده باشد. bootstrap شکست‌خورده را کورکورانه تکرار نکنید.
