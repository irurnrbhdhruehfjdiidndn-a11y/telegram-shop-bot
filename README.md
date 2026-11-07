# 💎 Genius Premium Store: Telegram Shop Bot

UPI QR payment, balance codes, wallet, products (file/APK/video/text/keys), categories, stock keys,
My Orders, deposit bonus, ban system, admin panel, auto backup, website dashboard.
Render par chalta hai, settings sirf **Render Environment** me.

## 📁 Files
| File | Kaam |
|---|---|
| `bot.py` | Poora bot + website |
| `requirements.txt` | Libraries |
| `render_env.txt` | Render me paste karne wali settings (copy-paste ke liye) |
| `.python-version`, `.gitignore` | Render / GitHub ke liye, inhe mat chhedna |

## 🧠 Env (Environment Variable) kya hota hai?
Bot ki settings (token, UPI, database link) jo **Render ke andar** bharte hain, GitHub par nahi jaati.
Render me ek table hoti hai: **Key** (naam) aur **Value** (tumhari jankari).

---

## ✅ Step 1: Token aur ID
1. Telegram me **@BotFather** → `/newbot` → naam aur username do → **token** copy karo.
2. **@userinfobot** ko `/start` bhejo → jo **Id** number de wahi tumhara `ADMIN_ID`.

## ✅ Step 2: Render par Database
1. https://render.com par GitHub se login.
2. **New → PostgreSQL** → Name `shopdb` → Plan **Free** → Create.
3. Page par **Internal Database URL** copy karo (`postgresql://...` wala lamba link).

## ✅ Step 3: GitHub par upload
1. ZIP ko pehle **extract** karo (GitHub zip khud nahi kholta).
2. GitHub par **New repository** → naam `telegram-shop-bot` → **Private** → Create.
3. **Add file → Upload files** → saari files daalo → **Commit changes**.

## ✅ Step 4: Render par Bot (Web Service)
1. **New → Web Service** → apni repo chuno.
2. **Build Command:** `pip install -r requirements.txt`
3. **Start Command:** `python bot.py`
4. **Instance Type:** Free, aur Region database wala hi.
5. **Environment Variables** me ye daalo (`render_env.txt` me tayyar hai):

| Key | Value kya daalni hai |
|---|---|
| `BOT_TOKEN` | BotFather wala token |
| `ADMIN_ID` | Tumhara Telegram ID (number) |
| `DATABASE_URL` | Step 2 wala Internal Database URL |
| `UPI_ID` | Tumhara UPI ID, jaise `naam@okaxis` |
| `UPI_NAME` | UPI pe jo naam dikhta hai |
| `SUPPORT_USERNAME` | `@tumhara_username` |
| `BRAND_NAME` | `Genius Premium Store` |
| `DASH_KEY` | Koi lamba password (private dashboard ke liye) |
| `DB_LIFETIME_DAYS` | `30` |

**Daalne ka tarika:** Key wale box me naam, Value wale box me jankari, phir **Add**. Agar **"Add from .env"** button dikhe,
to `render_env.txt` ki saari lines ek saath paste kar do (pehle values badal lena).

Dhyan: Key ka naam bilkul wahi likho (bade akshar + underscore). Value me quotes ya extra space mat lagao.
Sabse zaroori 5: `BOT_TOKEN`, `ADMIN_ID`, `DATABASE_URL`, `UPI_ID`, `UPI_NAME`.

6. **Create Web Service** dabao. 3-5 minute me Logs me `Bot start ho gaya` dikhna chahiye.

## ✅ Step 5: Bot ko 24/7 jagaye rakho
Render free service 15 minute baad so jati hai.
1. https://uptimerobot.com par free account.
2. **Add New Monitor** → HTTP(s) → URL: `https://TUMHARA-APP.onrender.com/health` → Interval **5 minutes**.

## ✅ Step 6: Test
- Bot me `/start` → menu. `/admin` → admin panel (sirf tumhe).
- **Add Product** se product daalo, phir user menu se kharid ke dekho.
- Browser me Render URL → website dashboard.
- Private dashboard: `https://TUMHARA-APP.onrender.com/admin?key=TUMHARA_DASH_KEY`

---

## 💳 Payment kaise kaam karta hai
1. User **Add Balance** → amount chunta hai → QR / UPI ID dikhta hai.
2. User pay karke UTR + screenshot bhejta hai.
3. Tumhe **Approve / Reject** buttons aate hain.
4. **Bank app me paisa dekhkar** Approve dabao → user ko one-time balance code milta hai.
5. User **Redeem Code** karta hai → wallet me balance → koi bhi product kharid sakta hai.

## 🛠 Admin commands (sirf ADMIN_ID wale ke liye)
| Command | Kaam | Example |
|---|---|---|
| `/admin` | Admin panel kholta hai | `/admin` |
| `/user ID` | User ka balance, orders, deposits, ban status | `/user 8233305377` |
| `/ban ID` | User ko block karta hai | `/ban 8233305377` |
| `/unban ID` | Block hataata hai | `/unban 8233305377` |
| `/addbal ID AMOUNT` | Seedha balance jodta hai | `/addbal 8233305377 100` |
| `/addbal ID -AMOUNT` | Balance kaat leta hai (minus ke saath) | `/addbal 8233305377 -50` |
| `/delcode CODE` | Unused balance code cancel karta hai | `/delcode 95854B64` |
| `/restore` | Backup file ke caption me likho, data wapas aata hai | file + caption `/restore` |
| `/cancel` | Chal rahe kaam ko rokta hai | `/cancel` |

**Ban karne se kya hota hai:**
- User bot me kuch nahi kar paata, har button aur message par "Tumhara account band hai" aata hai.
- Broadcast usko nahi jata.
- Uska balance database me waisa hi rehta hai, aur usko koi notification nahi jata.
- **Ban se code cancel nahi hota.** Code alag se `/delcode` se cancel karna padta hai.

## 🚨 Galti se Approve ho gaya (fake payment) to kya karo
1. `/ban USER_ID` se uska account band karo.
2. `/delcode CODE` se wo code cancel karo (code Approve wale message me likha hota hai, jaise `APPROVED • code: 95854B64`).
3. Agar bot bole "code pehle hi redeem ho chuka hai", to `/user USER_ID` se balance dekho aur `/addbal USER_ID -AMOUNT` se kaat lo.
4. Uska UTR dobara use nahi ho sakta, wo apne aap block rehta hai.

**Bachne ke liye:** Approve tabhi dabao jab bank app me paisa aur wahi UTR dikh jaye. Screenshot par bharosa mat karo.

---

## 🔄 Har mahine Database renew (IMPORTANT)
Render ka free Postgres **30 din** me expire hota hai (phir 14 din grace, phir delete).
Bot 7 aur 2 din pehle warning + backup bhejta hai, aur har 12 ghante me bhi backup bhejta hai.

1. Panel me **💾 Backup DB** dabao, file save rakho.
2. Render me **naya Free Postgres** banao → **Internal Database URL** copy karo.
3. Web Service → **Environment** → `DATABASE_URL` ki value naye link se badlo → **Save** (redeploy ho jayega).
4. Bot me wo backup `.json` file bhejo, **caption me `/restore`** likho. Users, balance, products, orders, codes sab wapas.
5. Purana database delete kar do (free me ek hi allowed hai).

## 🧯 Dikkat aaye to
| Problem | Solution |
|---|---|
| `Render Environment me DATABASE_URL daalo` | Env me `DATABASE_URL` nahi daala ya naam galat likha |
| `Render Environment me BOT_TOKEN aur ADMIN_ID daalo` | `BOT_TOKEN` ya `ADMIN_ID` env me nahi hai |
| Build fail (Python / psycopg) | Env me `PYTHON_VERSION` = `3.12.8` daalo |
| Bot reply nahi de raha | Render Logs dekho; token ya ADMIN_ID galat to nahi |
| `Conflict: terminated by other getUpdates` | Bot ek hi jagah chalao (laptop band karo) |
| Bot kabhi kabhi der se reply de | UptimeRobot monitor lagao (Step 5) |
| Admin panel nahi khulta | `ADMIN_ID` apne asli ID se match karo |

## 🔒 Safety
- Token, `DASH_KEY` aur backup `.json` kisi ko mat dikhao. Backup me users ka data aur unused codes hote hain.
- Token leak ho to BotFather me `/revoke` karo, Render env me naya token daalo.
- Approve sirf bank me paisa dekhkar karo.
- Sirf wahi cheezein becho jo tumhari apni ho ya jinka resell right ho.
