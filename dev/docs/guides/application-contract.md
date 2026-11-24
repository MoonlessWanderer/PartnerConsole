# مشخصات پنل همکاری (یک فایل برای ایجنت)

کاربر گفته **شروع کن**. این تنها فایل دستور کار توست. طبق همین سند بساز.

---

## مأموریت

یک **پنل همکاری B2B** بساز که:
- **ادمین** پنل(های) V2Board، محصول، تعرفه، همکار و موجودی را مدیریت کند
- **همکار** با موجودی‌اش اشتراک بسازد/مدیریت کند و لینک subscribe بگیرد و بفروشد

**نساز:** فروشگاه عمومی، پنل مشتری نهایی، درگاه پرداخت همکار، دامنه اختصاصی، وایت‌لیبل (فاز ۲ است).

---

## استک (اجباری)

| لایه | تکنولوژی |
|------|-----------|
| Backend | **Python 3.12+ · FastAPI · SQLAlchemy 2 · Alembic · Pydantic v2 · httpx** |
| Auth اپ | JWT جدا برای ادمین و همکار |
| DB | PostgreSQL (prod) ؛ SQLite فقط برای dev اگر لازم |
| Frontend | **Next.js + TypeScript + Tailwind · RTL فارسی** |
| پکیج Python | `uv` یا `pyproject.toml` / `requirements.txt` |
| UI زبان/جهت | **فارسی · راست‌به‌چپ (dir=rtl)** همه‌جا |
| تاریخ | **تقویم شمسی کامل** (نمایش و ورودی؛ نه میلادی برای کاربر) |
| فونت | **فونت ایرانی محلی** (مثلاً Vazirmatn) — فایل‌ها داخل ریپو |
| CDN / استوریج خارجی | **ممنوع در runtime** برای فونت/CSS/JS/تصویر UI — self-hosted یا bundled |
| ریسپانسیو | **کامل** موبایل / تبلت / دسکتاپ (ادمین و همکار) |

ساختار پیشنهادی:
```
backend/app/{main.py,core,models,schemas,api,services,integrations}
backend/alembic/
dev/docs/guides/application-contract.md
```
فرانت: ریپو الان Next در **ریشه** دارد (با `/design`). همان را به پنل همکاری تبدیل کن؛ مجبور نیستی پوشه `frontend/` جدا بسازی.

---

## V2Board

- ریپو: https://github.com/wyx2685/v2board  
- مرتبط: https://github.com/wyx2685/V2bX · https://github.com/wyx2685/v2node  

پایه API: `/api/v1`  
لاگین ادمین پنل: `POST /api/v1/passport/auth/login` → `auth_data` (باید `is_admin`)  
ادمین پنل زیر مسیر امن: `/api/v1/{secure_path}/...` با هدر `Authorization: <auth_data>`

عملیات لازم روی یوزر پنل:
| کار | endpoint تقریبی |
|-----|-----------------|
| ساخت | `POST .../user/generate` (`email_prefix`, `email_suffix` بدون `@`, `plan_id`, `expired_at`) |
| ویرایش | `POST .../user/update` (همیشه با `id` + فیلدهای اجباری؛ ببین حیاتی) |
| ریست لینک | `POST .../user/resetSecret` (`id`) |
| حذف | `POST .../user/delUser` (`id`) |
| لیست (+لینک) | `GET .../user/fetch` — فیلتر با `id` یا `email`؛ فیلد `subscribe_url` اینجاست |
| جزئیات ترافیک | `GET .../user/getUserInfoById` — لینک ندارد؛ برای لینک `fetch` با فیلتر `id=v2_user_id` |
| پلن‌ها | `GET .../plan/fetch` |

**حیاتی (از کد واقعی wyx2685/v2board):**
1. موقع ساخت برای V2Board ایمیل ساخته می‌شود: `username + '@' + product.email_domain` (`email_prefix=username`, `email_suffix=email_domain` **بدون** `@`). مثال: `ali01@gold.example.com`.  
   **هشدار:** validation پنل `email:strict` است؛ دامنه باید قبول شود. از `.local` پرهیز کن مگر پنل شما قبول کند — در UI به ادمین هشدار بده.
2. **در DB ما ایمیل ذخیره نکن.** فقط `v2_user_id` (آیدی عددی یوزر پنل) + `username` + `product_id`. اگر API به فیلد `email` نیاز داشت، همان لحظه از `username + '@' + product.email_domain` بساز.
3. `username` فقط `a-z` `0-9` `_` ؛ طول ۳–۳۲ ؛ یکتایی عملی با ایمیل ساخته‌شده روی همان پنل.
4. `generate` معمولاً فقط `{data:true}` می‌دهد؛ `transfer_enable` را از پلن می‌گیرد و **`speed_limit` را ست نمی‌کند**. بلافاصله بعد از ساخت یک‌بار با ایمیل `fetch` کن → **`id` را بردار و فقط همان را ذخیره کن**، سپس با همان `id`، `update` بزن و حداقل این‌ها را override کن: `transfer_enable`, `expired_at`, `device_limit`, `speed_limit` (دو تای آخر از پلن محصول).
5. **بعد از ساخت، همه عملیات فقط با `v2_user_id`:** `update` / `resetSecret` / `delUser` / همگام‌سازی. دیگر با ایمیل lookup نکن (مگر بازیابی اضطراری).
6. **تلهٔ `update`:** اگر `plan_id` نفرستی، کنترلر پنل `group_id` را `null` می‌کند. در **هر** `update` حتماً `plan_id` + فیلدهای اجباری `email` (بازسازی‌شده), `banned`, `is_admin`, `is_staff` را بفرست. `is_admin`/`is_staff` همیشه ۰. برای ریست ترافیک: `u=0`, `d=0`.
7. پلن فقط برای دسترسی نود (`group_id`) است؛ حجم و زمان مال سیستم ماست.
8. GB در پنل = بایت با `GB * 1024**3` (همان `1073741824`).
9. ریست خودکار ترافیک پلن باید خاموش باشد: `reset_traffic_method = 2` روی پلن V2Board. اگر `null` باشد به تنظیم سراسری برمی‌گردد — موقع اتصال محصول چک/هشدار به ادمین بده.
10. **مصرف (`u+d`) و `subscribe_url` را در DB ما ذخیره نکن.**  
    **UI:** لیست اشتراک فقط از DB ما (username، محصول، سقف، انقضا، وضعیت) — بدون مصرف/لینک.  
    **جزئیات:** با `v2_user_id` live از پنل مصرف + لینک را بگیر (`getUserInfoById` برای `u/d`؛ لینک با `user/fetch` و فیلتر `id = v2_user_id` چون `subscribe_url` آنجاست — نیازی به ذخیره/فیلتر ایمیل نیست). کاهش/حذف/ریست ترافیک همان لحظه live می‌خوانند. بعد از `resetSecret` دوباره fetch کن تا لینک جدید بیاید. URL را خودت نساز مگر fallback اضطراری.
11. client با `httpx`؛ اگر `auth_data` منقضی شد دوباره login؛ credential پنل رمزنگاری‌شده در DB.

کلون مرجع برای خواندن کد:
```bash
git clone --depth 1 https://github.com/wyx2685/v2board.git /tmp/v2board
```

---

## قوانین محصول (قفل v1.9 — دقیق رعایت کن)

### موجودی همکار
1. **تومان** — سراسری؛ فقط ادمین ± می‌کند؛ برای کمبود خرید و ریست ترافیک  
2. **اعتبار GB** — **per-product**  
3. **اعتبار روز** — **per-product**  
موجودی منفی برای هیچ‌کدام مجاز نیست.

### تعرفه محصول
- `price_per_gb` (تومان/GB) — خرید حجم، و همچنین هزینه ریست مصرف  
- `price_per_day` (تومان/روز)  
- **فیلد جدا برای ریست نداریم** (`price_reset_traffic` حذف شد)
```
toman_paid = floor(GB_paid * price_per_gb) + floor(days_paid * price_per_day)
```

### ساخت / افزایش روی محصول X
1. **ساخت جدید:** GB (≥۱) + تاریخ پایان (تقویم) الزامی است → `need_days` از مبدأ محاسبه شود  
   **افزایش:** فقط ابعاد تغییرکرده (مثلاً فقط GB یا فقط تاریخ پایان)؛ برای بعدِ تغییرنیافته `paid/credit = 0`  
2. اول از `credit_gb[X]` و `credit_days[X]`  
3. کمبود از تومان با نرخ **همان محصول X**  
4. ناکافی → رد  
5. اعمال روی V2Board  
همیشه اول اعتبار، بعد تومان (بدون تیک «فقط نقدی»).

### زمان (UI + محاسبه)
- همکار **تاریخ پایان شمسی** را از **تقویم شمسی** می‌گیرد  
- میانبر: +۳۱ / +۶۲ / +۹۳ روز (از مبدأ معتبر همان حالت)  
- جدید یا منقضی: مبدأ = امروز ؛ تاریخ باید > امروز  
- فعال (تمدید): مبدأ = انقضای فعلی ؛ تاریخ باید > انقضا  
- ۱ ماه = ۳۱ روز  
- نمایش همه تاریخ‌ها در UI شمسی کامل است؛ ذخیره داخلی unix timestamp (ثانیه)
- **فرمول روز (اجباری، یکسان همه‌جا):**  
  `need_days = max(0, (end_date - base_date).days)` با تاریخ‌های تقویمی (بدون اعشار)  
  `days_left = max(0, floor((expired_at - now) / 86400))`  
  منطقه زمانی کسب‌وکار: **`Asia/Tehran`** برای «امروز» و تبدیل تاریخ شمسی↔timestamp پایان روز

### کاهش حجم/زمان روی X
→ برگشت به اعتبارهای **همان محصول X** (نه تومان)  
→ GB برگشتی: `max(0, old_cap - new_cap)`  
→ روز برگشتی: `max(0, old_days_left - new_days_left)` با همان فرمول `days_left`  
→ اگر مصرف فعلی (`u+d`) > سقف حجم جدید → رد  
→ تاریخ پایان جدید باید همچنان > امروز باشد (وگرنه رد)

### حذف از X
1. مصرف را **live** از V2Board بخوان (`u`,`d`) — ذخیره نکن  
2. `credit_gb[X] += max(0, traffic_cap_gb - used_gb)`  (`used_gb = (u+d)/1024**3`)  
3. `credit_days[X] += days_left` با فرمول بالا  
4. `delUser` با `id` یوزر پنل  
بدون برگشت تومان. اگر ردیف `PartnerProductCredit` نبود، بساز سپس اضافه کن.

### ریست
- لینک: رایگان (`resetSecret`)؛ بعدش live لینک جدید را از پنل بخوان  
- ترافیک:
  1. مصرف live از پنل: `used_gb = (u+d) / 1024**3`
  2. اگر `used_gb <= 0` → رد (چیزی برای ریست نیست)
  3. هزینه: `floor(used_gb * price_per_gb)` از تومان سراسری با نرخ **همان محصول**
  4. اعمال `u=0,d=0` روی V2Board
  5. سقف (`traffic_cap_gb`) و اعتبار GB/روز عوض نشوند  
  مثال: سقف ۲۰، مصرف ۱۰ → هزینه = `floor(10 * price_per_gb)`

### مسدود
- مسدود اشتراک ≠ حذف ؛ زمان می‌گذرد ؛ رفع توسط همکار یا ادمین  
- مسدود همکار: لاگین بسته + همه اشتراک‌ها مسدود با **علامت علت**  
- رفع مسدود همکار: فقط همان‌هایی که با آن علت بسته شدند باز شوند  

### سایر
- محصول خاموش: ساخت جدید نه؛ مدیریت قبلی بله  
- یادداشت مشتری روی اشتراک  
- شناسه username بعد ساخت عوض نمی‌شود  
- ایمیل / `traffic_used_bytes` / `subscribe_url` در DB ما ذخیره نشود؛ لینک به پنل فقط با `v2_user_id`؛ مصرف و لینک همیشه live از پنل
- تک‌ادمین برای MVP — اولین ادمین را از ENV بساز (`ADMIN_USERNAME` / `ADMIN_PASSWORD`) اگر جدول خالی بود  
- `device_limit` و `speed_limit` را از پلن V2Board روی یوزر کپی کن  
- ریست ترافیک: هزینه = مصرف‌شده × `price_per_gb` همان محصول (نه تعرفه جدا) 
- fail API بعد از کسر → **rollback** کامل  
- قفل تراکنش روی همکار+اشتراک در اعمال پولی  

---

## مدل داده (جداول)

همه مبالغ تومان: عدد صحیح ≥۰ با `floor`. زمان: unix ثانیه یا timestamptz؛ منطق کسب‌وکار `Asia/Tehran`.

### `admin_users`
| ستون | نوع | توضیح |
|------|------|--------|
| id | PK | |
| username | string unique | |
| password_hash | string | |
| created_at | ts | |

### `panels`
| ستون | نوع | توضیح |
|------|------|--------|
| id | PK | |
| name | string | نام نمایشی |
| base_url | string | مثلاً `https://panel.example.com` |
| secure_path | string | مسیر ادمین V2Board |
| admin_email | string | لاگین ادمین پنل |
| admin_password_enc | string | رمزنگاری‌شده |
| status | enum/string | active / disabled / error |
| last_ok_at | ts nullable | آخرین تست موفق |
| created_at / updated_at | ts | |

### `products`
| ستون | نوع | توضیح |
|------|------|--------|
| id | PK | |
| panel_id | FK → panels | |
| plan_id | int | پلن V2Board روی همان پنل |
| name | string | |
| email_domain | string | بدون `@` |
| price_per_gb | int | تومان / GB (خرید و ریست مصرف) |
| price_per_day | int | تومان / روز |
| active | bool | خاموش = ساخت جدید ممنوع |
| created_at / updated_at | ts | |
| UNIQUE | (panel_id, plan_id) پیشنهادی | یا حداقل یکتایی منطقی در UI |

### `partners`
| ستون | نوع | توضیح |
|------|------|--------|
| id | PK | |
| username | string unique | لاگین همکار |
| password_hash | string | |
| balance_toman | int ≥0 | تومان سراسری |
| banned | bool | |
| created_at / updated_at | ts | |

### `partner_product_credits`
| ستون | نوع | توضیح |
|------|------|--------|
| id | PK | |
| partner_id | FK → partners | |
| product_id | FK → products | |
| credit_gb | numeric/int ≥0 | فقط برای همان محصول |
| credit_days | int ≥0 | فقط برای همان محصول |
| UNIQUE | (partner_id, product_id) | |

### `subscriptions`
| ستون | نوع | توضیح |
|------|------|--------|
| id | PK | آیدی داخلی ما |
| partner_id | FK → partners | |
| product_id | FK → products | از روی آن پنل مشخص می‌شود |
| username | string | ثابت بعد از ساخت |
| v2_user_id | int | لینک پایدار به یوزر V2Board |
| note | text nullable | یادداشت مشتری |
| traffic_cap_gb | numeric/int ≥1 | سقف حجم — منبع حقیقت billing |
| expired_at | unix/ts | انقضا |
| banned | bool | مسدود اشتراک |
| blocked_by_partner_ban | bool | به‌خاطر مسدود همکار |
| created_at / updated_at | ts | |
| UNIQUE | (product_id, username) | |
| UNIQUE | (product_id, v2_user_id) | کافی است چون product→یک panel |

**ذخیره نشود:** `email` · `traffic_used_bytes` · `subscribe_url` (همیشه live از پنل با `v2_user_id`)

### `wallet_ledgers`
| ستون | نوع | توضیح |
|------|------|--------|
| id | PK | |
| partner_id | FK → partners | |
| asset | enum | `toman` / `gb` / `days` |
| amount | signed number | + شارژ / − کسر |
| product_id | FK nullable | برای gb/days اجباری؛ برای toman معمولاً null (مگر دلیل خرید محصول) |
| subscription_id | FK nullable | اگر مربوط به یک اشتراک |
| reason | string | create / increase / decrease / delete_refund / reset_traffic / admin_adjust / … |
| actor_type | enum | `admin` / `partner` / `system` |
| actor_id | int nullable | |
| created_at | ts | |

حذف اشتراک: ردیف `subscriptions` پاک می‌شود (یا soft-delete اگر خواستی)؛ `subscription_id` در ledger با `ON DELETE SET NULL` بماند تا تاریخچه مالی نشکند.

---

## صفحات MVP

**ادمین:** ورود · داشبورد · پنل‌ها (+تست اتصال) · محصولات · همکاران (+تنظیم تومان و اعتبار per محصول) · اشتراک‌ها · ledger  

**همکار:** ورود · داشبورد (تومان + اعتبارها per محصول) · لیست اشتراک · ساخت · جزئیات+عملیات · تاریخچه  

### الزامات UI (اجباری)
- کل اپ **فارسی** و **`dir="rtl"`** / `lang="fa"`
- تمام تاریخ‌های کاربر: **شمسی کامل** (روز/ماه/سال؛ تقویم انتخاب تاریخ هم شمسی)
- محاسبات داخلی می‌تواند UTC/timestamp باشد؛ فقط لایه نمایش و ورودی کاربر شمسی است
- فونت ایرانی را **داخل ریپو** بگذار (مثلاً `public/fonts/vazirmatn/*.woff2`) و با `@font-face` لود کن
- **هیچ CDN یا استوریج خارجی در runtime** برای فونت/استاتیک/اسکریپت UI نزن (نه Google Fonts، نه jsDelivr، نه S3 عمومی برای UI). پکیج npm که موقع **build** داخل باندل می‌شود مجاز است؛ در اجرا نباید از اینترنت فونت/اسکریپت بگیرد.
- کتابخانه تقویم شمسی و فونت باید self-hosted / bundled باشند
- اعداد پول و تاریخ ترجیحاً با ارقام فارسی در UI
- سایدبار تیره، CTA حدود `#0F766E`، جدول‌محور؛ از `/design` الگو بگیر اگر هست

### ریسپانسیو کامل (اجباری)
باید روی **موبایل (~۳۶۰–۴۳۰px)**، **تبلت (~۷۶۸px)** و **دسکتاپ (≥۱۲۸۰px)** بدون اسکرول افقیِ شکسته و بدون قطع شدن اکشن‌ها کار کند (هم ادمین، هم همکار).

حداقل‌ها:
- **منوی موبایل (همبرگری):** در موبایل/تبلت سایدبار ثابت نباشد. آیکن همبرگر در Topbar؛ با لمس، منو از **راست صفحه به چپ** باز شود (RTL)، با لایه تیره پشت‌صحنه؛ با زدن بیرون منو یا دکمه بستن، جمع شود. روی دسکتاپ همان سایدبار ثابت بماند.
- **جدول‌ها:** در موبایل اسکرول افقی داخل کارت، یا کارت‌ستونی / لیست فشرده؛ دکمه‌های عملیات در دسترس بمانند
- **فرم ساخت اشتراک:** تک‌ستونه روی موبایل؛ خلاصه هزینه چسبان پایین یا بالای دکمه ثبت
- **مودال‌ها:** عرض مناسب موبایل، قابل بستن، بدون بیرون‌زدن از viewport
- **تقویم شمسی:** روی موبایل قابل لمس و کامل قابل استفاده
- **تاچ:** دکمه‌ها حداقل حدود ۴۰–۴۴px ارتفاع؛ فاصله کافی بین اکشن‌های خطرناک
- **Topbar:** عنوان + همبرگر روی موبایل مرتب باشد
- **تست دستی DoD:** حداقل یک صفحه لیست + یک فرم + یک مودال پولی را در عرض ۳۷۵ و ۱۴۴۰ چک کن؛ همبرگر را هم باز/بسته تست کن

---

## ترتیب ساخت

1. FastAPI + DB + Alembic + JWT ادمین/همکار  
2. Panel + V2BoardClient  
3. Product  
4. Partner + ledger تومان/اعتبار  
5. سرویس billing (تقویم، اعتبار، تومان)  
6. Subscription end-to-end با پنل  
7. مسدود همکار / حذف / ریست‌ها  
8. فرانت Next  
9. تست + README + `.env.example`  

---

## Definition of Done

- [ ] اتصال و تست پنل V2Board  
- [ ] محصول با plan و دامنه و قیمت GB/روز  
- [ ] شارژ تومان و اعتبار per-product توسط ادمین  
- [ ] ساخت اشتراک واقعی + override حجم/زمان + لینک  
- [ ] افزایش/کاهش/حذف طبق اعتبار per-product  
- [ ] مسدود اشتراک و مسدود همکار  
- [ ] ریست لینک رایگان / ریست ترافیک با `used_gb × price_per_gb` 
- [ ] UI فارسی RTL ادمین و همکار قابل استفاده  
- [ ] تاریخ شمسی کامل در لیست‌ها، جزئیات و تقویم ساخت/تمدید  
- [ ] فونت ایرانی self-hosted؛ بدون CDN/استوریج خارجی برای استاتیک UI  
- [ ] ریسپانسیو کامل موبایل/تبلت/دسکتاپ (همبرگر راست‌به‌چپ، جدول، فرم، مودال، تقویم شمسی)  
- [ ] README اجرا · تست billing با mock httpx  

---

## خارج از scope

چند ادمین · پنل مشتری · درگاه · دامنه اختصاصی · پورسانت · تبدیل اعتبار به تومان · انتقال اعتبار بین محصولات  

---

## خروجی ریپو

بک‌اند Python را در پوشه `backend/` بساز. فرانت را برای پنل همکاری آماده کن (گالری `/design` را می‌توانی نگه داری).  
سکرت واقعی commit نکن. پورت‌های غیرپیش‌فرض استفاده کن.  
فونت‌ها را commit کن (یا با اسکریپت نصب محلی از پکیج npm به `public/fonts` کپی کن — در runtime به اینترنت وابسته نباشد).

**الان شروع کن و MVP فاز ۱ را طبق همین فایل پیاده کن.**
