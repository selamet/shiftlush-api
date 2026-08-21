# Elevator Maintenance Tracking — Faz 1 Teknik Şartnamesi

> Claude Code'a girdi olarak verilmek üzere yazılmıştır.
> **Kapsam:** Firma kaydı, kullanıcı/rol, adres verisi + harita, müşteri/bina/asansör kaydı, sözleşme, QR üretimi.
> Bakım planı, bakım formu, arıza takibi, faturalama ve mobil uygulama bu fazın dışındadır — ancak veri modeli bunları engellemeyecek şekilde kurulmalıdır.

---

## 0. İki temel kural

Bu iki karar dokümanın tamamını etkiler. Claude Code'a en başta ve net verilmelidir.

### 0.1 Dil ayrımı

**Kodda Türkçe hiçbir şey bulunmaz. Arayüzde İngilizce hiçbir şey bulunmaz.**

| Nerede | Dil |
|---|---|
| Tablo ve kolon adları | İngilizce |
| Model, sınıf, fonksiyon, değişken adları | İngilizce |
| Enum değerleri (veritabanında saklanan) | İngilizce |
| API endpoint yolları ve alan adları | İngilizce |
| Kod yorumları, commit mesajları, dokümantasyon | İngilizce |
| Dosya ve dizin adları | İngilizce |
| Hata kodları | İngilizce, `SCREAMING_SNAKE_CASE` |
| Git branch adları | İngilizce |
| **Kullanıcının gördüğü her metin** | **Türkçe** |
| E-posta içerikleri | Türkçe |
| PDF etiket çıktıları | Türkçe |

Türkçe metinlerin tek yaşadığı yer **çeviri dosyalarıdır**. Bir `.py`, `.ts` veya `.tsx` dosyasının içinde Türkçe bir dize (string) görüyorsanız hata var demektir.

### 0.2 Ayrık frontend ve backend

İki **ayrı depo**, iki ayrı derleme, iki ayrı dağıtım:

- `elevator-api` — Python/Django, yalnızca JSON API. HTML üretmez, şablon kullanmaz.
- `elevator-web` — Next.js, yalnızca istemci. Veritabanına erişmez.

Aralarındaki tek bağ **OpenAPI sözleşmesidir**. Detay: bölüm 14.

---

## 1. Faz 1 kapsam sınırı

### Yapılacak
- Firma (tenant) kaydı ve ayarları
- Kullanıcı yönetimi, rol/yetki, davet akışı, oturum yönetimi
- Türkiye il/ilçe/mahalle referans verisi + arama + harita ile konum seçimi
- Müşteri ve müşteri iletişim kişileri
- Site → Bina → Asansör hiyerarşisi, asansör teknik künyesi
- Sözleşme kaydı ve sözleşme–asansör ilişkisi
- Asansör başına QR token üretimi ve yazdırılabilir etiket çıktısı
- Dosya yükleme (asansör fotoğrafı, CE belgesi, imzalı sözleşme PDF'i)
- Audit log
- Türkçe arayüz altyapısı (i18n)

### Yapılmayacak
Bakım planı üretimi, bakım formu/kaydı, arıza yönetimi, periyodik kontrol modülü (alanlar şemada olacak, ekran olmayacak), faturalama, mobil uygulama, müşteri portalı, bildirim/hatırlatma motoru.

**Kural:** "İleride lazım olur" diye ekstra modül yazdırmayın. Sadece veri modelinde ilgili alanları hazır bırakın.

---

## 2. Teknoloji kararları

### 2.1 Backend — `elevator-api`

| Katman | Seçim | Gerekçe |
|---|---|---|
| Veritabanı | PostgreSQL 16+ | JSONB (audit log), pg_trgm (mahalle arama), ileride PostGIS |
| Dil | **Python 3.14** | Ağustos 2026 itibarıyla en güncel kararlı sürüm. `.python-version` dosyasıyla sabitlenir |
| Framework | **Django 6.1** + Django REST Framework | Django 6.x, Python 3.12–3.14 destekler. Sürüm politikası için bkz. 2.5 |
| ORM / migration | Django ORM + Django migrations | Ayrı migration aracı gerekmez |
| API sözleşmesi | **Elle yazılan OpenAPI 3.1 spesifikasyonu** | drf-spectacular kullanılmıyor — bkz. 2.6 |
| Filtreleme | django-filter | Liste endpoint'lerindeki filtre/sıralama tekrarını önler |
| Auth | djangorestframework-simplejwt + Argon2 hasher | Access/refresh, rotation, blacklist hazır |
| CORS | django-cors-headers | Ayrık frontend zorunlu kılıyor |
| Dosya | **Cloudflare R2** + django-storages + boto3 (dev: MinIO) | S3 uyumlu, çıkış trafiği ücretsiz — bkz. 2.7 |
| Çeviri | Django `gettext` + `locale/tr/` | E-posta metinleri için, bkz. 13.3 |
| QR / PDF | `qrcode` + `WeasyPrint` | |
| Test | pytest + pytest-django + factory_boy | |
| Kod kalitesi | ruff (lint + format) + mypy | Black/isort/flake8 yerine tek araç |
| Paket yönetimi | uv | Deterministik kilit dosyası |

### 2.2 Frontend — `elevator-web`

| Katman | Seçim |
|---|---|
| Framework | Next.js 15 (App Router) + TypeScript |
| UI | TailwindCSS + shadcn/ui |
| Form | React Hook Form + Zod |
| Veri çekme | TanStack Query |
| i18n | next-intl |
| API tipleri | openapi-typescript — elle yazılan spec'ten üretilir, elle düzenlenmez |
| Harita | Leaflet + OpenStreetMap + Nominatim |
| Tablo | TanStack Table |
| Test | Vitest + Testing Library + Playwright (kritik akışlar) |

### 2.3 Neden FastAPI değil?

| | Django + DRF | FastAPI |
|---|---|---|
| CRUD hızı | ModelSerializer + ViewSet ile bir kaynak ~40 satır | Her endpoint elle |
| Migration | Dahili, otomatik üretim | Alembic ayrı kurulur |
| Yetki sistemi | Permission sınıfları dahili | Sıfırdan yazılır |
| Admin paneli | Hazır — adres verisi ve destek için değerli | Yok |
| Async | Kısmi (Faz 1'de gerekmiyor) | Doğal |

Faz 1'de tek bir gerçek zamanlı işlem yok; async avantajı burada işe yaramaz. **FastAPI'yi ancak ekip Django bilmiyorsa seçin** — o durumda SQLAlchemy 2.0 + Alembic + Pydantic v2 ile aynı şartname uygulanır, ama auth/tenant/yetki iş yükü belirgin artar.

### 2.4 Kullanılmaması gerekenler
- **MongoDB / NoSQL** — tamamen ilişkisel bir domain, referans bütünlüğü kritik
- **Firebase** — multi-tenant yetki modeli için uygun değil
- **Flask** — auth, migration, admin, permission'ı elle yazmak demek
- **Django template katmanı ile sunucu tarafı HTML** — API-only karar verildi. Tek istisna: QR etiket PDF'i ve e-posta gövdeleri.
- **`float` ile para** — `DecimalField(max_digits=12, decimal_places=2)`
- **ORM'siz ham SQL** — migration takibi kaybolur (istisna: adres verisi toplu yükleme)
- **Faz 1'de Celery, Redis, async view, WebSocket** — Django 6'nın dahili Tasks çerçevesi Faz 2'de Celery ihtiyacını zaten ortadan kaldırıyor (bkz. 2.5)

### 2.5 Django 6 sürüm notları

**Hangi 6.x?** Django 6.1 kullanın — 6.x serisinin güncel sürümü.

**Bilinmesi gereken:** Django 6.0 ve 6.1 **LTS değildir**, destek pencereleri kısadır. Bu serinin LTS sürümü **6.2** olacak ve Nisan 2027'de çıkması bekleniyor. Yani:

- Bugün 6.1 ile başlayın.
- 6.2 çıktığında **hemen geçin.** 6.1 → 6.2 küçük bir sıçramadır, ertelemeyin.
- 6.2'ye geçmezseniz birkaç ay içinde desteklenmeyen bir Django üzerinde kalırsınız.
- Bunu takvime yazın; bir yıl sonra "neden 6.1'deyiz" sorusu sorulmasın.

Alternatif olarak Django 5.2 LTS (Nisan 2028'e kadar destekli) ile başlanabilir, ama yeni bir projede iki yıl geriden başlamanın karşılığı yok — üstelik 6.0'ın getirdiği iki özellik bu projeye doğrudan yarıyor.

**Django 6'nın bu projeye doğrudan faydası olan özellikleri:**

| Özellik | Bu projede karşılığı |
|---|---|
| Dahili **Tasks** çerçevesi | Faz 2'deki bakım planı üretimi, sözleşme bitiş uyarısı ve e-posta kuyruğu için **Celery + Redis kurmaya gerek kalmayacak.** Faz 1'de kullanılmaz ama mimari kararı bugünden verin: arka plan işleri `django.tasks` ile yazılacak. |
| Dahili **CSP** desteği | `ContentSecurityPolicyMiddleware` ile içerik enjeksiyonu koruması. API-only backend'de sınırlı fayda ama admin paneli için değerli. |
| Modernleşmiş e-posta API'si | Davet ve şifre sıfırlama e-postaları yeni `EmailMessage` API'si ile yazılır, eski MIME sınıfları kullanılmaz. |
| Şablon partial'ları | QR etiket şablonunda tek etiketi `{% partialdef %}` ile tanımlayıp 12'lik grid'de tekrar edin. |

**Dikkat edilecek kırıcı değişiklikler:**
- `DEFAULT_AUTO_FIELD` varsayılanı `BigAutoField` oldu. Biz UUID birincil anahtar kullandığımız için etkilenmiyoruz, ama `settings` içinde açıkça belirtin.
- `django.core.mail` fonksiyonlarında konumsal argümanlar kullanımdan kaldırıldı — anahtar kelimeli argüman kullanın.
- Özel ORM ifadeleri parametreleri liste yerine **tuple** olarak döndürmeli.

### 2.6 API sözleşmesi — drf-spectacular olmadan

drf-spectacular kaldırıldı. Ancak **sözleşme ihtiyacı kalkmadı** — iki ayrı depo arasındaki tek bağ oydu. Yerine geçen yaklaşım:

**Spec-first (sözleşme önce):** `openapi/v1.yaml` dosyası **elle yazılır** ve tek doğruluk kaynağıdır. Kod ondan türetilmez, ama koda karşı **doğrulanır.**

| | Kod-önce (drf-spectacular) | Spec-önce (seçilen) |
|---|---|---|
| Bakım | Otomatik, sıfır emek | Elle — her endpoint değişikliğinde spec güncellenir |
| Kayma riski | Düşük | **Yüksek** — CI ile kontrol edilmezse kaçınılmaz |
| Tasarım disiplini | Zayıf; API kodun yan ürünü olur | Güçlü; iki depo kod yazılmadan sözleşmede anlaşır |
| Frontend paralel çalışma | Backend bitene kadar bekler | Spec yazılınca hemen başlar |

**Kayma riskini CI kapatır — bu zorunludur, opsiyonel değil:**

1. `schemathesis` (veya eşdeğeri) backend test paketinde çalışır: `openapi/v1.yaml` içindeki her endpoint gerçek API'ye karşı çağrılır, yanıt şemaya uymuyorsa **test kırılır.**
2. Spec'te olmayan bir URL route'u varsa veya route'u olmayan bir spec girdisi varsa **test kırılır.** Bu kontrol basit bir test olarak yazılır, Django'nun URL çözümleyicisi ile spec yolları karşılaştırılır.
3. `openapi/v1.yaml` değişmiş ama `CHANGELOG-API.md` güncellenmemişse uyarı üretilir.

Bu üç kontrol olmadan spec-first yaklaşımı üç ay içinde çöker ve elinizde yalan söyleyen bir doküman kalır.

**Eğer bu bakım yükünü istemiyorsanız tek gerçek alternatif DRF'i bırakmaktır.** Django Ninja, Pydantic tabanlıdır ve OpenAPI üretimi çerçevenin içindedir — ek paket gerekmez. Karşılığında DRF'in ViewSet, permission ve django-filter ekosistemini kaybedersiniz; bölüm 6'daki yetki matrisi ve bölüm 8'deki serializer kuralları yeniden yazılır. Bu bir tercihtir, ama **karar bugün verilmeli** — 12 endpoint yazıldıktan sonra geçiş yapmak istemezsiniz.

### 2.7 Cloudflare R2

Dosya depolama olarak Cloudflare R2 kullanılacak.

**Neden uygun:**
- **S3 uyumlu API** — `django-storages` + `boto3` değişmeden çalışır. Tek fark `endpoint_url` ayarı.
- **Çıkış trafiği ücretsiz.** Bu proje için önemli: Faz 2'de her bakım kaydına 2–4 fotoğraf eklenecek. 500 asansörlü bir firma yılda ~20.000 fotoğraf üretir. S3'te bunları okumak (müşteri portalı, mobil, rapor) sürekli çıkış maliyeti demektir; R2'de sıfır.
- İmzalı URL (presigned) desteği var — bölüm 8.6'daki doğrudan yükleme akışı aynen çalışır.

**Yapılandırma notları:**
- `endpoint_url = https://<account_id>.r2.cloudflarestorage.com`
- Bucket **private** kalır. Public bucket veya public custom domain **kullanılmaz** — erişim yalnızca süreli imzalı URL ile.
- **Bucket CORS ayarı zorunlu.** Tarayıcıdan doğrudan yükleme yapacaksanız R2 bucket'ında `PUT` için frontend origin'i izin listesine eklenmeli. Bu unutulduğunda hata mesajı yanıltıcıdır ve saatlerce aranır.
- İmzalı URL süresi: yükleme 15 dakika, indirme 5 dakika. Uzun süreli URL üretmeyin — kopyalanıp paylaşılır.
- Fiyatlandırma depolama + işlem sayısı üzerinden; okuma/yazma işlemlerini gereksiz tekrarlamayın (örneğin liste ekranında her satır için imzalı URL üretmeyin, yalnızca görüntülenen küçük resimler için üretin).
- **Yerel geliştirmede R2 kullanılmaz** — MinIO ile devam edin. R2'nin yerel öykünücüsü yok ve geliştirici makinelerinden gerçek bucket'a yazmak test verisi kirliliği yaratır.

**KVKK uyarısı — bu ciddi bir konu:**

R2 varsayılan olarak verinizi Türkiye dışında saklar. Yükleyeceğiniz dosyalar arasında **kişisel veri içerenler var**: imzalı sözleşme PDF'leri (TC kimlik numarası, imza), kimlik fotokopisi ekleri, bina yöneticisi bilgileri.

KVKK kapsamında kişisel verinin yurt dışına aktarımı serbest değildir; belirli koşullara bağlıdır. Bu yüzden:

- R2'nin **yargı bölgesi kısıtlaması** özelliğini kullanarak bucket'ı AB'ye sabitleyin — belirsiz bir "otomatik" konumdan iyidir.
- **İki ayrı bucket kurmayı değerlendirin:** kişisel veri içermeyen dosyalar (asansör fotoğrafı, CE belgesi, firma logosu) R2'de; kişisel veri içerenler (imzalı sözleşme, kimlik belgesi) Türkiye'de barındırılan bir sağlayıcıda. `attachment.category` alanı bu ayrımı zaten yapabiliyor.
- Hangi kararı verirseniz verin, **aydınlatma metninde açıkça belirtin** ve bir hukukçuya danıştırın. Bu bir mühendislik kararı değil, uyum kararıdır.

`attachment` modeline `storage_backend` alanı ekleyin (`r2`, `local`, `tr_provider`). Bugün tek değer kullanılsa bile, sonradan ayrıştırmak istediğinizde migration + dosya taşıma yapmadan geçiş yapabilirsiniz.

---

## 3. Mimari kararlar (tartışmasız)

### 3.1 Multi-tenancy
- Tek veritabanı, paylaşımlı şema. Tenant ayracı: `company_id`.
- Referans tabloları (`province`, `district`, `neighborhood`) hariç **her iş tablosunda `company_id` zorunlu**.
- `company_id` **asla** request body/query'den alınmaz. Her zaman JWT'den okunur.

**Django'da uygulama:**
- Ortak soyut model `CompanyOwnedModel` — `company` ForeignKey + tenant-aware manager, `abstract = True`.
- İstek başına firma bilgisi `contextvars.ContextVar` içinde tutulur; bir middleware JWT'den okuyup set eder. **Thread-local kullanmayın** — async'e geçildiğinde sessizce bozulur ve bir firmanın verisi başkasına sızabilir.
- Varsayılan manager (`objects`) her sorguya otomatik `company_id` filtresi ekler. Filtresiz erişim için ayrı ve **bilinçli olarak dikkat çekici** bir isim: `unscoped`. Kod incelemesinde göze batmalı.
- `objects` varsayılan manager olmalı (`Meta.base_manager_name` dahil), aksi halde ilişki üzerinden erişimde (`building.elevator_set`) filtre atlanır.
- `save()` sırasında nesnenin `company_id`'si aktif context ile eşleşmiyorsa istisna fırlatılır.
- Ek katman olarak PostgreSQL RLS önerilir, Faz 1'de opsiyonel.

### 3.2 Birincil anahtar
- **UUID v7** (zamana göre sıralanabilir, index parçalamaz).
- Python stdlib'de `uuid7` yok — `uuid-utils` paketini kullanın. `uuid.uuid4()` **kullanmayın**, rastgele UUID büyük tablolarda B-tree index'i parçalar.
- `models.UUIDField(primary_key=True, default=uuid7, editable=False)`
- Sıralı integer ID dışarı açılmaz. Django'nun varsayılan `AutoField`'ını devre dışı bırakın.
- `registration_number`, `contract_number` gibi iş anahtarları primary key olmaz, ayrı unique index alır.

### 3.3 Silme
- Hard delete **yoktur**. Tüm iş tablolarında `is_deleted` + `deleted_at`.
- Model `delete()` metodu override edilir, soft delete uygular. Gerçek silme için ayrı `hard_delete()`, sadece yönetim komutlarından çağrılır.
- **Django tuzağı:** `QuerySet.delete()` model `delete()`'ini çağırmaz, doğrudan SQL üretir. Manager'da `delete()`'i de override edin, yoksa toplu silmede kayıtlar gerçekten kaybolur.
- İşten ayrılan kullanıcı silinmez → `is_active = False`.
- `on_delete=models.PROTECT`. **`CASCADE` kullanmayın** — tek bir yanlış silme zinciri tüm asansör geçmişini götürür.

### 3.4 Zaman ve para
- Tüm zaman damgaları `timestamptz`, **UTC** saklanır. `TIME_ZONE = "UTC"`, `USE_TZ = True`.
- Yerelleştirme yalnızca frontend'de yapılır (`Europe/Istanbul`). Backend hiçbir yerde yerel saat üretmez.
- Sadece tarih olan alanlar `DateField`, saat taşımaz.
- Para: `DecimalField(max_digits=12, decimal_places=2)` + ayrı `currency` (varsayılan `TRY`).
- Python'da `Decimal` ile çalışın, `float`'a **hiç** dönüştürmeyin. DRF'de `COERCE_DECIMAL_TO_STRING = True` açık kalsın — para değerleri API'de string gitsin, JavaScript'in kayan nokta sorununu taşımayın.

### 3.5 Denetim izi
Her iş tablosunda: `created_at`, `updated_at`, `created_by`, `updated_by`.
Ayrıca merkezi `audit_log` tablosu — kim, ne zaman, hangi alanı değiştirdi (JSONB diff).

- `post_save` / `post_delete` sinyalleri ile merkezi bir fonksiyon çağrılır. Değişen alanları bulmak için nesnenin yüklenme anındaki hali `__init__`'te saklanır.
- `django-simple-history` alternatiftir ama her model için ayrı geçmiş tablosu üretir, şemayı ikiye katlar. Tek merkezi tablo daha yönetilebilir.
- Sinyaller `bulk_create` / `bulk_update` / `QuerySet.update()` çağrılarında **tetiklenmez.** Bu metotları iş kodunda kullanmayın.
- Hassas alanlar (`password`, `national_id`, `token_hash`) maskelenir; maskeleme listesi tek bir sabitte.

### 3.6 Enum'lar
Django `models.TextChoices` + veritabanı seviyesinde `CheckConstraint`.

- Saklanan değer **İngilizce, snake_case**: `passenger`, `gearless_electric`, `maintenance_only`.
- `TextChoices` etiketleri de İngilizce yazılır (Django admin için). **Türkçe karşılıklar frontend çeviri dosyalarında tutulur.**
- PostgreSQL native enum tipi kullanılmasın — yeni değer eklemek migration'da ağrılı. `varchar` + `CheckConstraint` yeterli.

### 3.7 İsimlendirme
- Tablo adları: `snake_case`, **tekil** (`elevator`, `contract`, `customer_contact`). Django varsayılanı `app_model` olduğu için `Meta.db_table` ile açıkça belirtin.
- Kolon adları: `snake_case`, İngilizce.
- Boolean alanlar `is_` veya `has_` ile başlar: `is_active`, `has_car_door`.
- Tarih alanları `_date`, zaman damgaları `_at` ile biter: `installation_date`, `created_at`.
- Sayı alanları birim taşır: `capacity_kg`, `speed_mps`, `pit_depth_mm`.
- Python: sınıflar `PascalCase`, fonksiyon/değişken `snake_case`. TypeScript: `PascalCase` / `camelCase`.
- API JSON alan adları **`snake_case`** — backend ile birebir aynı kalsın, dönüştürme katmanı eklemeyin.

---

## 4. Domain sözlüğü

Türkçe domain terimi ile İngilizce kod karşılığı arasındaki eşleme. **Bu tablo tek doğruluk kaynağıdır** — Claude Code'un kendi çevirisini uydurmasına izin vermeyin.

### 4.1 Varlıklar

| Türkçe | Kod | Not |
|---|---|---|
| Firma (tenant) | `Company` | |
| Kullanıcı | `User` | |
| Davet | `Invitation` | |
| Oturum / yenileme jetonu | `RefreshSession` | |
| Müşteri | `Customer` | Bina yönetimi, site yönetimi vb. |
| Müşteri iletişim kişisi | `CustomerContact` | |
| Site | `Complex` | Konut sitesi. `Site` kelimesi web sitesiyle karışır |
| Bina / blok | `Building` | |
| Asansör | `Elevator` | |
| Sözleşme | `Contract` | |
| Sözleşme–asansör ilişkisi | `ContractElevator` | |
| Dosya / ek | `Attachment` | `File` kullanmayın, Django `File` sınıfıyla çakışır |
| Denetim kaydı | `AuditLog` | |
| İl | `Province` | |
| İlçe | `District` | |
| Mahalle | `Neighborhood` | Köy ve belde de bu tabloda, `type` alanı ayırır |

### 4.2 Asansör terimleri

| Türkçe | Kod |
|---|---|
| Asansör kimlik numarası | `registration_number` |
| Firma iç kodu | `internal_code` |
| Kabin | `car` |
| Kabin kapısı | `car_door` |
| Kuyu dibi derinliği | `pit_depth_mm` |
| Üst boşluk / son kat yüksekliği | `headroom_mm` |
| Durak sayısı | `stop_count` |
| Giriş sayısı | `entrance_count` |
| Makine dairesi | `machine_room` |
| Tahrik tipi | `drive_type` |
| Kumanda tipi | `control_type` |
| Taşıma kapasitesi | `capacity_kg` / `capacity_persons` |
| Beyan hızı | `speed_mps` |
| Montaj tarihi | `installation_date` |
| İlk kullanım / piyasaya arz | `commissioning_date` |
| Periyodik kontrol | `inspection` |
| Muayene kuruluşu | `inspection_body` |
| Etiket rengi | `inspection_label` |
| Bakım periyodu | `maintenance_interval_days` |

### 4.3 Rol ve durum değerleri

| Türkçe | Kod |
|---|---|
| Firma sahibi | `owner` |
| Yönetici | `admin` |
| Operasyon | `operations` |
| Teknisyen | `technician` |
| Muhasebe | `accountant` |
| Aktif / askıya alınmış / mühürlü / hizmet dışı | `active` / `suspended` / `sealed` / `out_of_service` |
| Taslak / aktif / süresi dolmuş / feshedilmiş / yenilenmiş | `draft` / `active` / `expired` / `terminated` / `renewed` |
| Yeşil / mavi / sarı / kırmızı etiket | `green` / `blue` / `yellow` / `red` |

---

## 5. Veritabanı şeması

Tüm tablolarda ortak alanlar (soyut modellerden gelir):
`id` (uuid PK), `created_at`, `updated_at`, `created_by`, `updated_by`, `is_deleted`, `deleted_at`, `company` (referans tabloları hariç).

### 5.1 Referans tabloları (tenant'sız, global)

#### `province`
| Alan | Tip | Not |
|---|---|---|
| id | smallint PK | Plaka kodu 1–81 |
| name | varchar(50) | |
| name_normalized | varchar(50) | Arama için, bkz. 9.2 |

#### `district`
| Alan | Tip |
|---|---|
| id | integer PK |
| province | FK → province |
| name | varchar(80) |
| name_normalized | varchar(80) |

#### `neighborhood`
| Alan | Tip | Not |
|---|---|---|
| id | integer PK | |
| district | FK → district | |
| name | varchar(120) | |
| name_normalized | varchar(120) | |
| postal_code | char(5) | |
| type | choices | `neighborhood`, `village`, `town` |

**Index:** `(district_id)`, `(name_normalized) gin_trgm_ops`, `(postal_code)`

### 5.2 `company`

| Alan | Tip | Not |
|---|---|---|
| legal_name | varchar(200) | Resmi ticari unvan |
| display_name | varchar(80) | Arayüzde görünen ad |
| tax_office | varchar(100) | |
| tax_number | varchar(11) | VKN 10 hane / TCKN 11 hane |
| mersis_number | varchar(16) | |
| trade_registry_number | varchar(30) | |
| neighborhood | FK | |
| street | varchar(150) | |
| building_number | varchar(20) | Dış kapı no |
| unit_number | varchar(20) | İç kapı no |
| phone | varchar(20) | E.164 |
| email | varchar(150) | |
| website | varchar(150) | |
| logo | FK → attachment | |
| is_active | boolean | |

### 5.3 `user`

| Alan | Tip | Not |
|---|---|---|
| company | FK | |
| first_name | varchar(60) | |
| last_name | varchar(60) | |
| email | varchar(150) | **Global unique**, küçük harfe normalize |
| phone | varchar(20) | |
| password | varchar(255) | Davet kabul edilene kadar boş |
| role | choices | Bkz. 6.1 |
| is_active | boolean | |
| is_email_verified | boolean | |
| last_login_at | timestamptz | |
| failed_login_count | smallint | |
| locked_until | timestamptz | Brute-force koruması |
| national_id | char(11) | **Şifreli saklanır**, teknisyen için gerekli |
| certificate_number | varchar(50) | Mesleki yeterlilik belgesi |
| certificate_valid_until | date | Süre dolmadan uyarı için |

Django'nun varsayılan `User` modeli kullanılmaz. Bkz. 12.3.

### 5.4 `invitation`

| Alan | Tip | Not |
|---|---|---|
| company | FK | |
| email | varchar(150) | |
| first_name, last_name | varchar(60) | |
| role | choices | |
| token_hash | varchar(255) | **Token düz metin saklanmaz** |
| expires_at | timestamptz | Varsayılan 72 saat |
| accepted_at | timestamptz | |
| invited_by | FK → user | |

### 5.5 `refresh_session`

| Alan | Tip |
|---|---|
| user | FK |
| token_hash | varchar(255) |
| expires_at | timestamptz |
| revoked_at | timestamptz |
| user_agent | varchar(255) |
| ip_address | inet |

### 5.6 `customer`

| Alan | Tip | Not |
|---|---|---|
| company | FK | |
| type | choices | `complex_management`, `building_management`, `corporate`, `public`, `individual` |
| legal_name | varchar(200) | |
| tax_office | varchar(100) | |
| tax_number | varchar(11) | |
| national_id | char(11) | Şahıs müşterilerde, şifreli |
| phone, email | | |
| neighborhood | FK | Fatura adresi |
| street, building_number, unit_number | | |
| notes | text | |
| is_active | boolean | |

**Kritik:** Müşteri, binadan **ayrı** bir varlıktır. Bir site yönetimi 8 binanın müşterisi olabilir. Fatura ve sözleşme müşteriye bağlanır, binaya değil.

### 5.7 `customer_contact`

| Alan | Tip | Not |
|---|---|---|
| company, customer | FK | |
| full_name | varchar(120) | |
| role | choices | `manager`, `auditor`, `caretaker`, `technical_lead`, `accounting`, `other` |
| phone, email | | |
| is_primary | boolean | Müşteri başına en fazla bir tane (partial unique index) |
| notes | text | |

### 5.8 `complex`

Opsiyonel gruplama katmanı. Tek apartmanlar için `complex` NULL kalır.

| Alan | Tip |
|---|---|
| company, customer | FK |
| name | varchar(150) |
| neighborhood | FK |
| street, building_number | |
| latitude | decimal(9,6) |
| longitude | decimal(9,6) |
| notes | text |

### 5.9 `building`

| Alan | Tip | Not |
|---|---|---|
| company | FK | |
| complex | FK NULL | Siteye bağlıysa |
| customer | FK | |
| name | varchar(150) | "A Blok", "Yıldız Apartmanı" |
| type | choices | `residential`, `commercial`, `mixed_use`, `public`, `hospital`, `mall`, `hotel`, `school`, `industrial` |
| neighborhood | FK | |
| street | varchar(150) | |
| building_number | varchar(20) | |
| address_note | text | **Serbest metin — zorunlu bırakın.** Yeni siteler ve TOKİ bölgeleri veri setinde yoktur |
| latitude, longitude | decimal(9,6) | |
| floor_count | smallint | |
| unit_count | smallint | Daire/ofis sayısı |
| is_active | boolean | |

**Kural:** `complex` doluysa binanın `customer`'ı sitenin `customer`'ı ile aynı olmalı. Servis katmanında doğrulanır.

### 5.10 `elevator`

En kritik tablo. Alanların çoğu opsiyoneldir ama **şemada bulunmalıdır** — sonradan eklemek migration + geriye dönük veri girişi demektir.

**Kimlik**
| Alan | Tip | Not |
|---|---|---|
| company, building | FK | |
| registration_number | varchar(30) | Resmi kimlik no. `unique(company, registration_number)` |
| internal_code | varchar(30) | Firmanın kendi kodu |
| name | varchar(100) | "Sol asansör", "Yük asansörü" |
| qr_token | varchar(24) | **Global unique**, rastgele. Bkz. bölüm 11 |
| qr_token_generated_at | timestamptz | |

**Sınıflandırma**
| Alan | Değerler |
|---|---|
| category | `passenger`, `freight`, `passenger_freight`, `dumbwaiter`, `accessibility_platform`, `vehicle` |
| drive_type | `geared_electric`, `gearless_electric`, `hydraulic` |
| control_type | `simple_collective`, `down_collective`, `full_collective`, `group_control` |
| door_type | `automatic_center`, `automatic_side`, `semi_automatic`, `manual` |
| has_car_door | boolean — kabin kapısı olmaması periyodik kontrolde ağır uygunsuzluk sebebidir, raporlanabilir olmalı |
| machine_room | `present`, `absent`, `partial` |

**Teknik**
`capacity_kg`, `capacity_persons`, `stop_count`, `entrance_count`, `speed_mps` (decimal 4,2), `pit_depth_mm`, `headroom_mm`, `car_weight_kg`

**Üretim / montaj**
`brand`, `model`, `serial_number`, `manufacturer`, `installer`, `installation_date`, `commissioning_date`, `ce_certificate_number`, `warranty_end_date`

**Periyodik kontrol** *(Faz 1'de sadece alan, modül yok)*
| Alan | Tip |
|---|---|
| last_inspection_date | date |
| inspection_label | `green`, `blue`, `yellow`, `red`, `none` |
| next_inspection_date | date |
| inspection_body | varchar(150) |
| inspection_report_number | varchar(60) |

**Durum**
| Alan | Not |
|---|---|
| status | `active`, `suspended`, `sealed`, `out_of_service`, `uncontracted` |
| maintenance_interval_days | smallint, varsayılan 30, mevzuat gereği en fazla 30 |
| notes | text |

### 5.11 `contract`

| Alan | Tip | Not |
|---|---|---|
| company, customer | FK | |
| contract_number | varchar(30) | `unique(company, contract_number)`. Otomatik: `2026-0001` |
| status | choices | `draft`, `active`, `expired`, `terminated`, `renewed` |
| scope | choices | `maintenance_only`, `maintenance_and_repair`, `full_coverage` |
| start_date, end_date | date | |
| pricing_type | choices | `per_elevator`, `flat` |
| monthly_fee | decimal(12,2) | |
| currency | char(3), varsayılan `TRY` | |
| vat_rate | decimal(5,2) | |
| billing_period | choices | `monthly`, `quarterly`, `semiannual`, `annual` |
| auto_renew | boolean | |
| renewal_notice_days | smallint, varsayılan 60 | |
| previous_contract | FK → contract NULL | Yenileme zinciri |
| terminated_at | date | |
| termination_reason | text | |
| signed_document | FK → attachment | |
| notes | text | |

**Kısıtlar:** `end_date > start_date`. `status = terminated` ise `terminated_at` zorunlu. İkisi de `CheckConstraint` olarak veritabanına yazılır.

### 5.12 `contract_elevator`

| Alan | Tip |
|---|---|
| company, contract, elevator | FK |
| unit_price | decimal(12,2) |
| added_at | date |
| removed_at | date NULL |

**En önemli kısıt:** Bir asansör aynı anda **yalnızca bir aktif sözleşmede** olabilir. Veritabanı seviyesinde zorlayın:

```sql
CREATE UNIQUE INDEX uq_elevator_active_contract
ON contract_elevator (elevator_id)
WHERE removed_at IS NULL;
```

Django tarafında `UniqueConstraint(fields=["elevator"], condition=Q(removed_at__isnull=True))`.

Sözleşme bitince ilişki silinmez — `removed_at` doldurulur, geçmiş korunur.

### 5.13 `attachment`

| Alan | Tip | Not |
|---|---|---|
| company | FK | |
| object_type | choices | `elevator`, `building`, `contract`, `customer`, `company`, `user` |
| object_id | uuid | Polimorfik ilişki |
| category | choices | `photo`, `ce_certificate`, `declaration_of_conformity`, `permit`, `signed_contract`, `inspection_report`, `logo`, `other` |
| original_filename | varchar(255) | |
| mime_type | varchar(100) | |
| size_bytes | integer | |
| storage_key | varchar(500) | Nesne anahtarı |
| storage_backend | choices | `r2`, `local`, `tr_provider` — bkz. 2.7 KVKK notu |
| uploaded_by | FK → user | |

**Kurallar:** İçerik veritabanında saklanmaz. Yükleme öncesi MIME ve boyut doğrulaması (maks. 10 MB; jpeg, png, webp, pdf). Kullanıcının dosya adı sunucuda kullanılmaz — `storage_key` üretilir. İndirme her zaman imzalı geçici URL ile; bucket public olmaz.

**Django `GenericForeignKey` kullanmayın** — `content_type` tablosu üzerinden ekstra join getirir ve tenant filtresini karmaşıklaştırır. Basit `object_type` + `object_id` yeterli, doğrulama servis katmanında yapılır.

### 5.14 `audit_log`

| Alan | Tip |
|---|---|
| id | bigserial PK |
| company_id | uuid |
| user_id | uuid |
| table_name | varchar(60) |
| record_id | uuid |
| action | `create`, `update`, `delete` |
| old_values | jsonb |
| new_values | jsonb |
| ip_address | inet |
| user_agent | varchar(255) |
| created_at | timestamptz |

---

## 6. Yetkilendirme

### 6.1 Roller

| Kod | Türkçe etiket | Açıklama |
|---|---|---|
| `owner` | Firma sahibi | Tüm yetkiler + firma ayarları + kullanıcı pasifleştirme. Firma başına en az bir tane zorunlu. |
| `admin` | Yönetici | Firma ayarları hariç her şey. Kullanıcı davet edebilir. |
| `operations` | Operasyon | Müşteri/bina/asansör/sözleşme CRUD. Kullanıcı yönetimi yok. |
| `technician` | Teknisyen | Kendisine atanmış müşterilerin asansörlerini görüntüler. Yazma yetkisi yok. |
| `accountant` | Muhasebe | Müşteri ve sözleşme mali bilgilerini görür, teknik künyeyi görmez. |

### 6.2 Yetki matrisi

| Kaynak / İşlem | owner | admin | operations | technician | accountant |
|---|:---:|:---:|:---:|:---:|:---:|
| Firma ayarları — okuma | ✓ | ✓ | ✓ | ✓ | ✓ |
| Firma ayarları — yazma | ✓ | – | – | – | – |
| Kullanıcı listeleme | ✓ | ✓ | – | – | – |
| Kullanıcı davet/düzenleme | ✓ | ✓ | – | – | – |
| Kullanıcı pasifleştirme | ✓ | – | – | – | – |
| Müşteri — okuma | ✓ | ✓ | ✓ | kısıtlı¹ | ✓ |
| Müşteri — yazma | ✓ | ✓ | ✓ | – | – |
| Bina/Site — okuma | ✓ | ✓ | ✓ | kısıtlı¹ | – |
| Bina/Site — yazma | ✓ | ✓ | ✓ | – | – |
| Asansör — okuma | ✓ | ✓ | ✓ | kısıtlı¹ | – |
| Asansör — yazma | ✓ | ✓ | ✓ | – | – |
| Sözleşme — okuma | ✓ | ✓ | ✓ | – | ✓ |
| Sözleşme — yazma | ✓ | ✓ | ✓ | – | – |
| Sözleşme mali alanları | ✓ | ✓ | – | – | ✓ |
| QR etiket üretimi | ✓ | ✓ | ✓ | ✓ | – |
| Audit log görüntüleme | ✓ | ✓ | – | – | – |

¹ Teknisyen sadece kendisine atanmış müşterilerin kayıtlarını görür (`user_customer` ara tablosu).

### 6.3 Zorunlu kurallar
- Yetki kontrolü **her zaman sunucu tarafında**. Frontend'de menü gizlemek yetki değildir.
- `DEFAULT_PERMISSION_CLASSES = [IsAuthenticated]` — unutulan bir view açıkta kalmasın.
- Her ViewSet'te `permission_classes` açıkça belirtilir. Yetki adları sabit haritada: `elevator:write`, `contract:read`.
- Nesne bazlı kontrol (`has_object_permission`) teknisyen kısıtı için gerekli; `get_queryset` filtresine **ek olarak** uygulanır, yerine değil.
- Yetki kontrolü ve tenant kontrolü **iki ayrı katman**. Karıştırmayın.
- Firma başına en az bir aktif `owner` kalmalı — son sahibi pasifleştirme engellenir.

---

## 7. Kimlik doğrulama

### 7.1 Kayıt
1. Firma yetkilisi ad, soyad, e-posta, şifre, firma unvanı girer.
2. `Company` + `User` (rol `owner`) tek transaction'da oluşur.
3. E-posta doğrulama linki gönderilir (24 saat).
4. Doğrulanana kadar yetkiler kısıtlı — veri girişi yapılır, kullanıcı davet edilemez.

### 7.2 Çalışan daveti
1. Yönetici e-posta + ad + rol girer.
2. `Invitation` kaydı oluşur, kriptografik rastgele token üretilir. **Hash'i saklanır, düz hali sadece e-postaya gider.**
3. E-posta (Türkçe): davet linki, 72 saat geçerli.
4. Çalışan linke tıklar, **kendi şifresini belirler**, `User` aktifleşir.
5. Süresi dolan davet yeniden gönderilir, eski token geçersizleşir.

**Kesinlikle yapılmayacak:** Yöneticinin şifre belirlemesi, şifrenin e-postaya düz metin gönderilmesi, arayüzde yöneticiye gösterilmesi. Hem güvenlik açığı hem KVKK ihlali.

### 7.3 Şifre politikası
- Minimum 10 karakter. Karmaşıklık zorunluluğu (büyük/küçük/rakam/sembol) **koymayın** — uzunluk daha etkilidir.
- Yaygın şifre kara listesi kontrolü.
- Hash: **argon2id**. `PASSWORD_HASHERS` listesinin başına `Argon2PasswordHasher`, `argon2-cffi` bağımlılığı.
- Zorunlu periyodik şifre değiştirme **yok** — güvenliği azaltır.
- Sıfırlama: tek kullanımlık token, 1 saat, hash'li saklanır; kullanıldıktan sonra tüm oturumlar iptal.

### 7.4 Oturum
- Access token: JWT, 15 dakika, frontend'de **bellekte** tutulur (localStorage değil).
- Refresh token: 30 gün, httpOnly + Secure cookie, veritabanında hash'li, her kullanımda döndürülür (rotation).
- Kullanılmış refresh token tekrar gelirse o kullanıcının tüm oturumları iptal edilir.
- Giriş limiti: aynı e-posta + IP için 15 dakikada 5 deneme, sonra 15 dakika kilit.

**Ayrık dağıtımda kritik cookie tuzağı:** Frontend ve backend farklı origin'lerde. Cookie'nin çalışması için ikisi de **aynı kayıtlı üst alan adı** altında olmalı: `app.example.com` ve `api.example.com`. Bu durumda `SameSite=Lax` çalışır.

Tamamen farklı alan adları kullanırsanız (örn. frontend Vercel'de, backend Railway'de) `SameSite=None; Secure` zorunlu hale gelir, CSRF yüzeyi genişler ve bazı tarayıcı ayarlarında cookie tamamen engellenir. **Üretimde ikisini de aynı üst alan adına alın.** Bu bir DNS kararı, kod kararı değil — baştan planlayın.

---

## 8. API tasarımı ve sürümleme

### 8.1 Sürümleme stratejisi

**URL yolunda sürümleme:** `/api/v1/...`

Başlık (header) veya medya tipi sürümlemesi teorik olarak daha temizdir, ama operasyonel olarak acı verir: log'da hangi sürümün çağrıldığı görünmez, tarayıcıdan test edilemez, CDN önbelleği karışır, mobil istemci sürüm sabitlemesi zorlaşır. URL sürümlemesi bu üçünü de çözer.

Django tarafında:

```python
REST_FRAMEWORK = {
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ["v1"],
}
```

`ALLOWED_VERSIONS` listesinde olmayan bir sürüm istenirse 404 döner — sessizce varsayılana düşmez.

### 8.2 Neyin sürümleneceği

Bu, sürümlemenin en çok yanlış yapılan kısmı. **Yalnızca API katmanı sürümlenir. Model ve iş mantığı sürümlenmez.**

```
apps/elevators/
├── models.py            ← sürümlenmez
├── services.py          ← sürümlenmez
├── selectors.py         ← sürümlenmez
└── api/
    ├── v1/
    │   ├── serializers.py
    │   ├── views.py
    │   ├── filters.py
    │   └── urls.py
    └── v2/              ← ancak gerektiğinde açılır
        └── serializers.py
```

v2 çıktığında yalnızca değişen serializer/view kopyalanır; değişmeyenler v1'den içe aktarılır. **`services.py` asla çatallanmaz** — iş kuralı iki yerde yaşarsa biri mutlaka güncellenmeden kalır ve iki sürüm farklı davranmaya başlar. Bu, sürümlemenin gerçek maliyetinin ortaya çıktığı yerdir.

### 8.3 Neyin kırıcı değişiklik sayıldığı

| Değişiklik | Kırıcı mı? | Yapılacak |
|---|:---:|---|
| Yeni opsiyonel alan eklemek (yanıta) | Hayır | v1 içinde |
| Yeni endpoint eklemek | Hayır | v1 içinde |
| Yeni opsiyonel query parametresi | Hayır | v1 içinde |
| Yeni enum değeri eklemek | **Evet¹** | Dikkatli — bkz. dipnot |
| Alan silmek | Evet | v2 |
| Alan adını değiştirmek | Evet | v2 |
| Alan tipini değiştirmek | Evet | v2 |
| Enum değeri kaldırmak | Evet | v2 |
| İsteğe bağlı alanı zorunlu yapmak | Evet | v2 |
| Varsayılan değeri değiştirmek | Evet | v2 |
| Hata kodunu kaldırmak/yeniden adlandırmak | Evet | v2 |
| HTTP durum kodunu değiştirmek | Evet | v2 |
| Sayfalama varsayılanını değiştirmek | Evet | v2 |
| Yanıt sıralamasını değiştirmek | Evet² | v2 |

¹ Yeni enum değeri teknik olarak eklemedir, ama istemci `switch` bloğunda karşılıksız kalır ve arayüzde boş/bozuk görünür. **Frontend her enum için bilinmeyen değer davranışı tanımlamalıdır** (ham değeri göster, çökme). Bu kural konursa yeni enum değeri kırıcı olmaktan çıkar. Bölüm 10.2'de zorunlu kılındı.

² İstemci sıralamaya güveniyorsa. Varsayılan sıralamayı **açıkça belgeleyin ve değiştirmeyin.**

### 8.4 Sürüm yaşam döngüsü

```
active  →  deprecated  →  sunset  →  removed
```

- **Aynı anda en fazla iki sürüm canlı olur.** Üçüncüsü açılacaksa en eskisi kapatılır.
- Bir sürüm `deprecated` işaretlendiğinde **en az 6 ay** yaşamaya devam eder.
- Kullanımdan kaldırılan sürümün yanıtlarına standart başlıklar eklenir:

```http
Deprecation: true
Sunset: Sat, 01 Aug 2026 00:00:00 GMT
Link: <https://docs.example.com/api/migration/v2>; rel="deprecation"
```

- Kapatma tarihinden sonra istekler **410 Gone** + `API_VERSION_SUNSET` kodu alır. 404 dönmeyin — istemci "endpoint kayboldu" sanıp yanlış yere bakar.
- Kapatmadan önce o sürümü kullanan firmalara e-posta gider. Bunun için sürüm kullanımı log'lanmalı: her istekte `api_version` alanı erişim log'una yazılır.

### 8.5 API değişiklik günlüğü

Backend deposunda `CHANGELOG-API.md` bulunur, `keep-a-changelog` formatında. Her kırıcı olmayan ekleme bile buraya yazılır. Frontend ekibinin (ve ileride entegrasyon yapan müşterilerin) tek referansı budur.

CI'da `oasdiff` veya benzeri bir araç ile `openapi/v1.yaml` dosyasının bir önceki sürümü ile yeni hali karşılaştırılır:
- Kırıcı fark bulunursa ve sürüm artmadıysa **build kırılır.**
- Kırıcı olmayan fark bulunursa ve `CHANGELOG-API.md` güncellenmediyse uyarı üretilir.

### 8.6 Faz 1 endpoint envanteri

Tümü `/api/v1` önekli. `(genel)` işaretliler kimlik doğrulaması istemez.

**Kimlik doğrulama**
| Metot | Yol |
|---|---|
| POST | `/auth/register` (genel) |
| POST | `/auth/login` (genel) |
| POST | `/auth/refresh` (genel — cookie ile) |
| POST | `/auth/logout` |
| POST | `/auth/password-reset` (genel) |
| POST | `/auth/password-reset/confirm` (genel) |
| POST | `/auth/email/verify` (genel) |
| GET | `/auth/me` |

**Davetler**
| Metot | Yol |
|---|---|
| GET, POST | `/invitations` |
| DELETE | `/invitations/{id}` |
| POST | `/invitations/{id}/resend` |
| GET | `/invitations/verify/{token}` (genel) |
| POST | `/invitations/accept` (genel) |

**Firma ve kullanıcılar**
| Metot | Yol | Not |
|---|---|---|
| GET, PATCH | `/company` | Tekil kaynak — tenant örtük, `{id}` yok |
| GET, POST | `/users` | |
| GET, PATCH | `/users/{id}` | |
| POST | `/users/{id}/deactivate` | |
| PUT | `/users/{id}/customers` | Teknisyen müşteri ataması |

**Adres**
| Metot | Yol | Not |
|---|---|---|
| GET | `/provinces` | Salt okunur |
| GET | `/districts?province={id}` | |
| GET | `/neighborhoods?district={id}&search=` | En fazla 20 sonuç |
| GET | `/geocode/reverse?lat=&lng=` | Nominatim **backend üzerinden** çağrılır |

Reverse geocoding'i frontend'den doğrudan Nominatim'e yaptırmayın. Backend üzerinden geçirmek üç şey kazandırır: sonuçları önbellekleyebilirsiniz, sağlayıcı kotasını kontrol edebilirsiniz, ve sağlayıcıyı değiştirdiğinizde frontend'e dokunmazsınız.

**Müşteriler**
| Metot | Yol |
|---|---|
| GET, POST | `/customers` |
| GET, PATCH, DELETE | `/customers/{id}` |
| GET, POST | `/customers/{id}/contacts` |
| GET, PATCH, DELETE | `/customer-contacts/{id}` |

**Yapılar**
| Metot | Yol |
|---|---|
| GET, POST | `/complexes` |
| GET, PATCH, DELETE | `/complexes/{id}` |
| GET, POST | `/buildings` |
| GET, PATCH, DELETE | `/buildings/{id}` |

**Asansörler**
| Metot | Yol | Not |
|---|---|---|
| GET, POST | `/elevators` | |
| GET, PATCH, DELETE | `/elevators/{id}` | |
| POST | `/elevators/{id}/regenerate-qr` | Etiket kopyalandığında |
| GET | `/elevators/by-qr/{token}` | QR yönlendirmesi |
| POST | `/elevators/labels` | Toplu PDF üretimi, gövdede id listesi |

**Sözleşmeler**
| Metot | Yol |
|---|---|
| GET, POST | `/contracts` |
| GET, PATCH, DELETE | `/contracts/{id}` |
| POST | `/contracts/{id}/elevators` |
| DELETE | `/contracts/{id}/elevators/{elevator_id}` |
| POST | `/contracts/{id}/terminate` |
| POST | `/contracts/{id}/renew` |

`terminate` ve `renew` neden ayrı endpoint? Çünkü ikisi de birden fazla tabloya dokunan, doğrulama kuralı olan **iş işlemleridir** — `PATCH /contracts/{id}` ile `status: "terminated"` göndermek, fesih tarihi ve asansör ilişkilerinin kapatılması gibi yan etkileri istemcinin sorumluluğuna atar. Durum geçişleri sunucuda yaşamalı.

**Ekler**
| Metot | Yol | Not |
|---|---|---|
| POST | `/attachments/upload-url` | İmzalı yükleme URL'i üretir |
| POST | `/attachments` | Yükleme sonrası kaydı doğrular |
| GET | `/attachments/{id}/download-url` | İmzalı, süreli |
| DELETE | `/attachments/{id}` | |

Dosya baytları Django üzerinden **geçmez.** İstemci imzalı URL ile doğrudan S3'e yükler, sonra backend'e onaylatır. Aksi halde büyük dosyalarda uygulama sunucusu tıkanır.

**Denetim ve meta**
| Metot | Yol | Not |
|---|---|---|
| GET | `/audit-logs` | Filtreli, sayfalı |
| GET | `/schema/` | `openapi/v1.yaml` statik olarak sunulur (üretilmez) |
| GET | `/docs/` | Redoc/Swagger UI statik sayfası — **üretimde kapalı veya IP kısıtlı** |
| GET | `/health` | **Sürümsüz**, `/api` öneki yok |
| GET | `/ready` | **Sürümsüz** — veritabanı ve S3 bağlantısını kontrol eder |

`/health` ve `/ready` sürümlenmez; bunlar altyapı uç noktalarıdır, API sözleşmesinin parçası değildir.

### 8.7 İstek ve yanıt kuralları

- Kaynak adları çoğul İngilizce, `kebab-case` çok kelimeliyse: `/customer-contacts`, `/audit-logs`
- Alan adları `snake_case`, veritabanı ile birebir. Dönüştürme katmanı eklemeyin.
- GET (liste/detay), POST (oluştur), PATCH (kısmi güncelle), DELETE (soft delete)
- **PUT kullanmayın** — tek istisna `/users/{id}/customers` gibi bir koleksiyonun tamamını değiştiren işlemler.
- `PATCH` semantiği: **alan gönderilmediyse değişmez, `null` gönderildiyse temizlenir.** Bu ayrım serializer'da açıkça ele alınmalı, yoksa "boş bıraktım silinmedi" hataları çıkar.
- Zaman damgaları ISO 8601 UTC: `2026-08-21T14:30:00Z`
- Para değerleri **string**: `"1250.00"` (bkz. 3.4)

### 8.8 Listeleme

```
GET /api/v1/elevators?page=1&page_size=25&ordering=name&search=yildiz&building=<uuid>&status=active
```

```json
{
  "results": [],
  "pagination": { "page": 1, "page_size": 25, "total": 342, "total_pages": 14 }
}
```

- `page_size` varsayılan 25, **maksimum 100**. İstemcinin 10.000 istemesine izin vermeyin.
- Her endpoint'in varsayılan sıralaması belgelenir ve değiştirilmez (bkz. 8.3 dipnot 2).
- **N+1 sorgu bırakmayın** — her liste endpoint'inde `select_related` / `prefetch_related` zorunlu. CI'da `nplusone` veya `django-zen-queries` ile denetleyin.

### 8.9 Standart başlıklar

**Her istekte kabul edilen:**
| Başlık | Amaç |
|---|---|
| `Authorization: Bearer <token>` | Access token |
| `X-Request-ID` | İstemci üretir; yoksa sunucu üretir |
| `Idempotency-Key` | POST'larda opsiyonel, bkz. 8.10 |

**Her yanıtta dönen:**
| Başlık | Amaç |
|---|---|
| `X-Request-ID` | Log'larla eşleştirme |
| `X-API-Version` | Hangi sürümün cevapladığı |
| `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` | Kota durumu |
| `Retry-After` | 429 ve 503 yanıtlarında |

`X-Request-ID` her log satırına ve 500 yanıtının gövdesine yazılır. Kullanıcı "hata aldım" dediğinde ekrandaki kimliği söylemesi, log'da tek bir aramayla o isteğe ulaşmanızı sağlar.

### 8.10 Idempotency

POST istekleri için opsiyonel `Idempotency-Key` başlığı desteklenir. Aynı anahtarla gelen ikinci istek, ilkinin yanıtını **yeniden döndürür**, yeni kayıt oluşturmaz. Anahtarlar 24 saat saklanır.

Faz 1'de kritik değil ama `/contracts` ve `/elevators` oluşturma uçlarında uygulayın — mobil ve zayıf bağlantıda çift kayıt en sık şikayet konusudur. Faz 2'de bakım kaydı için zaten zorunlu olacak.

### 8.11 Hata formatı

Backend Türkçe metin **döndürmez.** Makine tarafından okunabilir kodlar döner, çeviriyi frontend yapar.

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "request_id": "01J8XQ...",
    "details": [
      { "field": "tax_number", "code": "INVALID_TAX_NUMBER" },
      { "field": "end_date", "code": "END_DATE_BEFORE_START_DATE" }
    ]
  }
}
```

- Hata kodları merkezi bir enum'da (`core/error_codes.py`) toplanır ve `openapi/v1.yaml` içinde **elle listelenir**. İkisinin senkron kaldığını doğrulayan bir test yazın: enum'daki her değer spec'te bulunmalı.
- Frontend `tr.json` içinde `errors.INVALID_TAX_NUMBER` eşlemesini tutar.
- Bilinmeyen kod gelirse frontend genel mesaj gösterir, kodu konsola loglar — beyaz ekran vermez.
- **Hata kodu kaldırmak veya yeniden adlandırmak kırıcı değişikliktir** (bkz. 8.3). Yeni kod eklemek değildir.

**HTTP durum kodları:**
| Kod | Kullanım |
|---|---|
| 400 | Doğrulama hatası |
| 401 | Kimlik yok veya token geçersiz |
| 403 | Yetki yok |
| 404 | Bulunamadı **veya tenant ihlali** |
| 409 | Çakışma (`RECORD_IN_USE`, `DUPLICATE_REGISTRATION_NUMBER`) |
| 410 | Kapatılmış API sürümü |
| 422 | İş kuralı ihlali |
| 429 | Kota aşımı |

**Her şeye 200 dönüp body'de `success: false` yazmayın.**

Özel `exception_handler` yazılır, `REST_FRAMEWORK["EXCEPTION_HANDLER"]` ile bağlanır:
- `ValidationError` → 400 + alan bazlı `details`
- `PermissionDenied` → 403; **tenant ihlali ise 404** (403, "bu kayıt var ama göremezsin" bilgisini sızdırır)
- `ProtectedError` → 409 + `RECORD_IN_USE`
- Beklenmeyen istisna → 500 + `request_id`; stack trace **istemciye gitmez**, log'a yazılır

### 8.12 Serializer kuralları

- Yazma ve okuma için **ayrı serializer**. Tek serializer'da `read_only` alanlarla oynamak, `company` gibi alanların yanlışlıkla yazılabilir kalmasına yol açar.
- `fields = "__all__"` **kullanmayın** — modele yeni hassas alan eklendiğinde otomatik API'ye sızar ve bu **sessiz bir kırıcı değişikliktir**. Alanları tek tek yazın.
- Serializer'da tanımsız alan gelirse **reddedilir**. DRF varsayılan olarak sessizce yok sayar; `validate()` içinde `initial_data` kontrolüyle kapatın.
- Doğrulama kurallarının tek kaynağı serializer'lardır. Algoritmik kontroller `core/validators.py` içinde tek yerde.

### 8.13 Güvenlik

- Rate limit: kimliksiz uçlarda 20 istek/dk/IP, kimlikli 300 istek/dk/kullanıcı. `/auth/login` ayrıca 5/15dk (bkz. 7.3).
- CORS: yalnızca bilinen origin'ler. `CORS_ALLOW_ALL_ORIGINS = True` **asla**.
- `/docs/` üretimde kapalı veya IP kısıtlı. Şema, tüm alan adlarınızı ve iş kurallarınızı ifşa eder.
- Güvenlik başlıkları açık (HSTS, `X-Content-Type-Options`, `Referrer-Policy`).

### 8.14 Dış entegrasyon API'si (Faz 3 — şimdi yapılmayacak)

İleride müşteri veya iş ortağı sistemlerine API açacaksanız, bugün alınacak iki karar var:

1. **Ayrı bir API yüzeyi kurmayın.** Aynı `/api/v1` uçları kullanılır, yalnızca kimlik doğrulama mekanizması farklı olur: kullanıcı JWT'si yerine firma bazlı API anahtarı veya OAuth2 client credentials. İkinci bir endpoint kümesi, iki kat bakım maliyeti demektir.
2. **Bugünkü tasarım kararları buna uygun olmalı:** hata kodları makine okunabilir (✓), sayfalama tutarlı (✓), alan adları kararlı (✓), sürümleme politikası yazılı (✓). Bu dört madde sağlandığı için Faz 3'te ek iş çıkmayacak.

Faz 1'de **hiçbir API anahtarı altyapısı yazılmaz.** Sadece bu kararı belgeleyip geçin.

---

## 9. Adres verisi

### 9.1 Kaynak ve yükleme
- Kaynak: NVİ Adres Kayıt Sistemi / PTT posta kodu veri setleri veya bunlardan türetilmiş açık veri setleri.
- Veri `apps/address/data/` altında CSV.
- Django yönetim komutu: `python manage.py load_address_data`
- **İdempotent** olmalı — `bulk_create(..., update_conflicts=True)` ile upsert.
- Hacim: 81 il, ~970 ilçe, ~50.000 mahalle/köy. 2000'lik partiler halinde `bulk_create`; tek tek `create()` dakikalar sürer.
- Audit sinyallerini tetiklemeyen tek yer burasıdır — referans verisi denetim gerektirmez.
- **Data migration olarak yazmayın.** 50.000 satırlık migration her test kurulumunda çalışır ve test süresini uçurur.
- Yılda bir güncelleme yeterli. Runtime'da dış API bağımlılığı **olmasın**.

### 9.2 Türkçe arama — kritik tuzak

Türkçe'de `İ` → `i` dönüşümü standart `lower()` ile **yanlış** çalışır. Kullanıcı "istanbul" yazdığında "İstanbul" bulunmaz.

Python'da `"İ".lower()` normal `i` değil, üzerine ayrı bir birleştirici nokta eklenmiş **iki kod noktalı** bir dizi üretir. Veritabanındaki `i` ile eşleşmez ve hata sessizce oluşur.

Çözüm: harf değişimi **önce**, `lower()` **sonra**:

```python
TR_TRANSLATION = str.maketrans("İIıŞşĞğÜüÖöÇç", "IIiSsGgUuOoCc")

def normalize(text: str) -> str:
    return text.translate(TR_TRANSLATION).lower().strip()
```

- `core/text.py` içinde tek bir yerde bulunur; hem yükleme hem arama aynı fonksiyonu çağırır.
- Her kayıtta `name_normalized` kolonu tutulur, yükleme sırasında doldurulur.
- `pg_trgm` extension açılır, `name_normalized` üzerine GIN index.
- Birim test zorunlu: `normalize("İSTANBUL") == "istanbul"`, `normalize("Şişli") == "sisli"`.

Frontend'de de aynı normalize gerekiyorsa TypeScript karşılığı yazılır ve **iki tarafta test edilir** — davranışları ayrışmamalı.

### 9.3 Arayüz davranışı
- İl: 81 kayıt, tam liste dropdown.
- İlçe: il seçilince yüklenir.
- Mahalle: **asla tam liste basılmaz.** İlçe seçilince typeahead açılır, en az 2 karakter, debounce 300 ms, en fazla 20 sonuç.
- Sonuç yoksa "mahalle bulunamadı, adres tarifi alanına yazın" yönlendirmesi — akış tıkanmaz.

### 9.4 Harita

İki yönlü çalışmalı:
1. **Adresten haritaya:** İl/ilçe/mahalle seçilince harita zoom yapar, kullanıcı pin'i sürükler. `latitude`/`longitude` kaydedilir.
2. **Haritadan adrese:** Pin bırakılınca reverse geocoding ile alanlar otomatik dolar. Kullanıcı dolan alanları **düzeltebilmeli** — geocoding sonucu öneridir, kilitlenmez.

**Notlar:**
- Harita sağlayıcısı bir soyutlama arkasında olsun (`MapProvider` arayüzü). Google Maps'e geçiş tek dosya değişikliği olmalı.
- Nominatim'in kullanım politikası var — üretimde kendi instance'ınızı çalıştırın veya ticari sağlayıcıya geçin.
- Nominatim'in mahalle adı ile veritabanınızdaki ad **birebir eşleşmeyebilir**. Fuzzy eşleştirme yapın (trigram benzerliği > 0.4); eşleşme yoksa alanları boş bırakıp kullanıcıya seçtirin. **Yanlış mahalleyi otomatik doldurmak, boş bırakmaktan kötüdür.**
- `latitude`/`longitude` zorunlu alan yapılmasın — saha ekibi bazen konumu bilmeden kayıt açar. Eksikse arayüzde uyarı gösterin.

---

## 10. Türkçe arayüz (i18n)

### 10.1 Temel kural
Frontend'de **hiçbir JSX/TS dosyasında Türkçe dize bulunmaz.** Tüm metinler `messages/tr.json` içinden `t()` ile gelir.

```tsx
// YANLIŞ
<Button>Kaydet</Button>

// DOĞRU
<Button>{t("common.save")}</Button>
```

`next-intl` kullanılır. Varsayılan ve tek dil `tr`; yapı çok dilli olacak şekilde kurulur ama Faz 1'de ikinci dil eklenmez.

### 10.2 Çeviri dosyası yapısı

```
messages/
└── tr.json
```

Anahtarlar hiyerarşik ve İngilizce:

```json
{
  "common": { "save": "Kaydet", "cancel": "İptal", "delete": "Sil" },
  "elevator": {
    "title": "Asansörler",
    "fields": { "registrationNumber": "Asansör kimlik no", "capacityKg": "Kapasite (kg)" },
    "category": { "passenger": "İnsan asansörü", "freight": "Yük asansörü" },
    "inspectionLabel": { "green": "Yeşil", "red": "Kırmızı" }
  },
  "errors": {
    "INVALID_TAX_NUMBER": "Vergi numarası geçersiz",
    "END_DATE_BEFORE_START_DATE": "Bitiş tarihi başlangıçtan önce olamaz"
  }
}
```

**Enum etiketleri burada yaşar** — veritabanında değil, backend'de değil. Backend `passenger` döner, frontend `elevator.category.passenger` anahtarıyla "İnsan asansörü" gösterir.

**Bilinmeyen enum değeri kuralı (zorunlu):** Frontend, çeviri dosyasında karşılığı olmayan bir enum değeri geldiğinde **çökmez ve boş göstermez** — ham değeri gösterir ve konsola uyarı yazar. Bu kural sayesinde backend yeni enum değeri eklediğinde (örneğin yeni bir asansör kategorisi) eski frontend sürümü çalışmaya devam eder ve bu bir kırıcı değişiklik olmaktan çıkar (bkz. 8.3). Bunu tek bir `enumLabel(namespace, value)` yardımcı fonksiyonunda uygulayın; bileşenler `t()` çağrısını doğrudan yapmasın.

### 10.3 Backend'de Türkçe metin

Backend yalnızca **iki yerde** Türkçe üretir; ikisi de kod değil, çeviri/şablon dosyasıdır:

1. **E-posta gövdeleri** — Django `gettext` + `locale/tr/LC_MESSAGES/django.po`. Kod içinde `_("Your invitation")` yazılır, Türkçe karşılığı `.po` dosyasında durur. `LANGUAGE_CODE = "tr"`.
2. **QR etiket PDF'i** — `templates/labels/elevator_label.html` içindeki metinler de `{% trans %}` ile geçer.

Bunlar dışında backend'den kullanıcıya gidecek hiçbir Türkçe metin yoktur. API yanıtlarındaki `message` alanları **kaldırılmıştır**, yerine `code` vardır.

### 10.4 Biçimlendirme

Tarih, sayı ve para biçimlendirmesi **yalnızca frontend'de**, `Intl` API ile:

- Tarih: `Intl.DateTimeFormat("tr-TR")` → `21.08.2026`
- Para: `Intl.NumberFormat("tr-TR", { style: "currency", currency: "TRY" })` → `1.250,00 ₺`
- Ondalık ayracı virgül, binlik ayracı nokta. **Elle string birleştirmeyin.**
- Backend UTC ISO 8601 döner (`2026-08-21T14:30:00Z`), frontend `Europe/Istanbul`'a çevirir.

### 10.5 Denetim
- CI'da bir lint kuralı: `src/` altındaki `.tsx`/`.ts` dosyalarında Türkçe karaktere (`ğüşıöçĞÜŞİÖÇ`) rastlanırsa build kırılır. Tek istisna `messages/`.
- Backend'de aynı kontrol `.py` dosyaları için çalışır; istisna `locale/` ve `templates/`.
- `tr.json` içinde kullanılmayan anahtar veya kodda karşılığı olmayan `t()` çağrısı varsa uyarı üretilir.

---

## 11. QR kod

### 11.1 Token
- Her asansör oluşturulduğunda `qr_token` üretilir: **nanoid, 12 karakter, URL-safe alfabe.**
- Token asansörün `id`'si veya `registration_number`'ı **olmaz** — tahmin edilebilir olur, rakip firma sıralı deneyerek veri kazır.
- Global unique. Değiştirilebilir olmalı (etiket kopyalandığında iptal için) — `qr_token_generated_at` tutulur.

### 11.2 URL
```
https://app.example.com/q/{qr_token}
```
- Giriş yapmış ve yetkili → asansör detayına yönlenir
- Giriş yapmamış → giriş sayfası, sonra aynı asansöre yönlenir
- Başka firmanın token'ı → **404** (403 değil)

### 11.3 Etiket çıktısı
- A4'te 3×4 = 12 etiket grid'i, PDF.
- Her etikette: QR, asansör adı, bina adı, asansör kimlik no, firma logosu ve telefonu — **Türkçe**.
- QR minimum 25×25 mm. Hata düzeltme seviyesi **H** (%30) — makine dairesinde loş ışıkta, kirli ve yıpranmış yüzeyde okunabilmeli.
- Toplu seçim: bina veya müşteri bazlı "tüm asansörlerin etiketini yazdır".

---

## 12. Backend proje yapısı

### 12.1 Dizin

```
elevator-api/
├── pyproject.toml
├── manage.py
├── config/
│   ├── settings/
│   │   ├── base.py
│   │   ├── development.py
│   │   ├── production.py
│   │   └── test.py
│   ├── urls.py
│   └── wsgi.py
├── core/
│   ├── models.py          # abstract models
│   ├── managers.py        # tenant-aware manager
│   ├── middleware.py      # company context
│   ├── permissions.py
│   ├── validators.py      # tax number, national id, phone
│   ├── text.py            # Turkish normalization
│   ├── exceptions.py      # error codes + handler
│   └── pagination.py
├── apps/
│   ├── companies/
│   ├── users/             # custom user, auth, invitations
│   ├── address/           # province/district/neighborhood + loader
│   ├── customers/
│   ├── properties/        # complex + building
│   ├── elevators/         # elevator + QR
│   ├── contracts/
│   ├── attachments/
│   └── audit/
├── locale/tr/LC_MESSAGES/
├── templates/labels/
├── openapi/
│   └── v1.yaml            # elle yazılan API sözleşmesi
├── .python-version        # 3.14
└── tests/
```

Her app içinde: `models.py`, `serializers.py`, `views.py`, `urls.py`, `filters.py`, `services.py`, `admin.py`, `tests/`.

**`services.py` kuralı:** Birden fazla modele dokunan veya transaction gerektiren iş mantığı (sözleşme oluşturma + asansör ilişkilendirme, davet kabulü + kullanıcı aktifleştirme) view'a değil servis fonksiyonuna yazılır. View sadece serileştirme ve yetki ile ilgilenir. Faz 2'de aynı mantık mobil API'den de çağrılacak.

### 12.2 Ortak soyut modeller

Sırayla şu üçü yazılır, tüm iş modelleri bunlardan türer:

- `TimeStampedModel` — `created_at`, `updated_at`, `created_by`, `updated_by`
- `SoftDeleteModel` — `is_deleted`, `deleted_at`, override edilmiş `delete()`, filtreli manager
- `CompanyOwnedModel` — `company` FK + tenant-aware manager

Bu üçü doğru yazılmazsa 40 tabloya tek tek alan eklemek zorunda kalırsınız. **Adım 2'de, diğer modellerden önce yazılır.**

### 12.3 Kullanıcı modeli

Django'nun varsayılan `User` modeli **kullanılmaz**. `AUTH_USER_MODEL` projenin **ilk migration'ından önce** özel modele işaret etmelidir.

- `AbstractBaseUser` + `PermissionsMixin`
- `USERNAME_FIELD = "email"` — kullanıcı adı yok, e-posta ile giriş
- `PASSWORD_HASHERS` başında `Argon2PasswordHasher`

**Uyarı:** `AUTH_USER_MODEL` ilk migration'dan sonra değiştirilemez. Sonradan düzeltmesi çok pahalı bir karardır.

### 12.4 Django admin

Admin **ürün arayüzü değildir**, iç operasyon aracıdır. Tamamen İngilizce kalır.

- Yalnızca `is_superuser` erişir. Firma kullanıcıları **asla** giremez.
- Kullanım: adres verisi kontrolü, destek taleplerinde kayıt inceleme, seed doğrulama.
- **Kritik uyarı:** Admin, 3.1'deki tenant filtresini **bypass eder**. Bunu bilinçli kabul edin; üretimde admin URL'ini rastgeleleştirin ve IP kısıtı koyun.

### 12.5 Ayarlar
- Ortam değişkenleri `django-environ` veya `pydantic-settings` ile okunur, `os.environ` doğrudan kullanılmaz.
- Üretim ayarları başlangıçta doğrulanır: `SECRET_KEY`, veritabanı URL'i, S3 anahtarları eksikse uygulama **açılmamalı**, varsayılana düşmemeli.
- `DEBUG` üretimde sabit `False`.
- `.env` repoya commit edilmez; `.env.example` bulunur.

### 12.6 Test
- `pytest-django` + `factory_boy`. Her model için factory.
- **Zorunlu test seti:** her ViewSet için (a) yetkisiz erişim reddi, (b) çapraz firma erişim reddi, (c) rol bazlı yetki matrisi doğrulaması.
- Kapsam hedefi: `core/` ve `services.py` içinde %90, genelinde %70.
- Testler gerçek PostgreSQL'e karşı çalışır — **SQLite kullanmayın**, JSONB ve partial index davranışları farklıdır.

---

## 13. Frontend proje yapısı

```
elevator-web/
├── package.json
├── messages/tr.json
├── src/
│   ├── app/
│   │   ├── (auth)/login, register, invite/[token]
│   │   ├── (dashboard)/customers, buildings, elevators, contracts, users, settings
│   │   └── q/[token]/          # QR yönlendirme
│   ├── components/
│   │   ├── ui/                 # shadcn
│   │   ├── forms/
│   │   └── map/
│   ├── lib/
│   │   ├── api/
│   │   │   ├── client.ts       # fetch wrapper, token refresh
│   │   │   ├── generated.ts    # ÜRETİLEN — elle düzenlenmez
│   │   │   └── endpoints/
│   │   ├── auth/
│   │   └── format.ts           # Intl sarmalayıcıları
│   └── types/
└── openapi/v1.yaml             # backend deposundan kopyalanır
```

**Kurallar:**
- `generated.ts` elle düzenlenmez, `.gitignore`'a **konmaz** (derleme backend'e bağımlı olmasın diye commit edilir).
- API çağrıları doğrudan bileşenden yapılmaz; `lib/api/endpoints/` altındaki fonksiyonlar üzerinden geçer.
- Access token React state/context'te tutulur, **localStorage'a yazılmaz** (XSS riski).
- Her liste sayfası sunucu tarafı sayfalama kullanır — 500 asansörlü firma var, istemcide filtreleme yapmayın.

---

## 14. İki depo arasındaki sözleşme

### 14.1 OpenAPI akışı

Spec elle yazıldığı için akış kod-önce yaklaşımın **tersidir**: spec önce gelir, iki taraf da ona uyar.

```
                openapi/v1.yaml
                (elle yazılır — tek doğruluk kaynağı)
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
  elevator-api                   elevator-web
  schemathesis ile               openapi-typescript ile
  yanıtlar doğrulanır            generated.ts üretilir
```

**Sıra:**
1. Yeni bir endpoint yazılmadan **önce** `openapi/v1.yaml` güncellenir ve gözden geçirilir. İki taraf sözleşmede anlaşır.
2. Backend endpoint'i yazar. CI'da `schemathesis` spec'teki her uç için gerçek yanıtı doğrular; uyumsuzsa **build kırılır**.
3. Frontend `npm run generate:api` ile `generated.ts` üretir ve paralel olarak ekranı yazar — backend'in bitmesini beklemez.

**Spec nerede yaşar?** Backend deposunda (`elevator-api/openapi/v1.yaml`) tutulur ve sürüm çıktığında frontend deposuna kopyalanır. Ayrı bir "contract" deposu açmayın — üç depo, iki depodan daha zor yönetilir.

**Kritik:** Frontend derlemesi **asla çalışan bir backend'e bağımlı olmamalı.** `v1.yaml` ve `generated.ts` her ikisi de frontend deposunda commit edilir. Aksi halde backend kapalıyken frontend derlenemez ve CI kırılganlaşır.

**Uyarı:** Elle yazılan spec, güncellenmediği anda yalan söylemeye başlar ve hiçbir uyarı vermez. 2.6'daki üç CI kontrolü bu yaklaşımın maliyeti değil, **ön koşuludur.** Kurulmayacaksa spec-first yapmayın.

### 14.2 Sürümleme

Politikanın tamamı bölüm 8.1–8.5'tedir. Depolar arası akışı ilgilendiren kısım:

- Spec dosyası **sürüm başına** ayrıdır: `openapi/v1.yaml`, ileride `openapi/v2.yaml`.
- Frontend hangi sürüme bağlı olduğunu `package.json` içinde belgeler ve tek seferde tek sürüm tüketir.
- Backend `v2` açtığında frontend hemen geçmek zorunda değildir — `v1` en az 6 ay yaşar (8.4).
- Frontend'in kullandığı sürüm `deprecated` işaretlendiğinde CI uyarı üretir; `sunset` tarihine 30 gün kalınca build kırılır.

### 14.3 Yerel geliştirme
- Backend `localhost:8000`, frontend `localhost:3000`.
- `CORS_ALLOWED_ORIGINS = ["http://localhost:3000"]`
- Frontend `NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1`
- Backend deposunda `docker-compose.yml` — Postgres + MinIO. Frontend'in Docker'a ihtiyacı yok.
- İki depo yan yana klonlanır; ortak bir `Makefile` veya `README` her ikisini de başlatan komutu belgeler.

### 14.4 Dağıtım
- Backend: konteyner (Docker), `gunicorn` + `whitenoise` (yalnızca admin statikleri için).
- Frontend: Node veya edge; `output: "standalone"`.
- **Aynı kayıtlı üst alan adı zorunlu:** `api.example.com` + `app.example.com`. Gerekçe bölüm 7.4'te.
- İki ayrı CI hattı, iki ayrı sürüm etiketi. Backend her zaman önce dağıtılır (geriye dönük uyumlu değişiklikte sorun olmaz).

---

## 15. Doğrulama kuralları

| Alan | Kural |
|---|---|
| `tax_number` (VKN) | 10 hane, resmi kontrol algoritması |
| `national_id` (TCKN) | 11 hane, resmi kontrol algoritması, ilk hane 0 olamaz |
| `phone` | E.164'e normalize. `0555 123 45 67`, `+905551234567`, `5551234567` → `+905551234567` |
| `email` | Küçük harfe çevrilir, kenar boşlukları temizlenir |
| `postal_code` | 5 hane rakam |
| `latitude` | -90 … 90 |
| `longitude` | -180 … 180 |
| `maintenance_interval_days` | 1–30 (mevzuat gereği aylık bakım zorunlu) |
| `capacity_kg` | > 0 |
| `stop_count` | 2–100 |
| `installation_date` | Gelecek tarih olamaz |
| `end_date` | `start_date`'ten büyük olmalı |

Algoritmik kontroller `core/validators.py` içinde tek yerde. Frontend'deki Zod şemaları yalnızca **anlık geri bildirim** içindir, güvenlik katmanı değildir.

---

## 16. KVKK ve güvenlik

- **Aydınlatma metni ve açık rıza** akışı Faz 1'de kurulmalı — sonradan geriye dönük rıza toplamak çok zordur.
- Kişisel veri barındıran alanlar: `user.national_id`, `customer.national_id`, `customer_contact` tablosunun tamamı.
- TC kimlik numaraları uygulama katmanında **şifrelenerek** saklanır (AES-256-GCM, anahtar secret manager'da).
- **Yurt dışına veri aktarımı:** Cloudflare R2 verileri Türkiye dışında saklar. Kişisel veri içeren dosyalar (imzalı sözleşme PDF'i, kimlik belgesi) için bölüm 2.7'deki uyarıyı okuyun ve bir hukukçuya danışın. Bu karar Faz 1'de verilmeli — sonradan dosya taşımak istemezsiniz.
- Veri saklama süresi politikası tanımlanır; soft delete'ten sonra kalıcı anonimleştirme süresi belirlenir (öneri: 5 yıl).
- Üretimde HTTPS zorunlu, HSTS açık.
- Veritabanı yedeği günlük otomatik, geri yükleme testi yapılmış.

---

## 17. Claude Code'a verilecek görev sırası

Tek bir dev prompt yerine **sıralı, doğrulanabilir adımlar**. Her adımdan sonra çalıştırıp kontrol edin.

### Backend deposu

0. **API sözleşmesi taslağı** — `openapi/v1.yaml`, bölüm 8.6'daki uçların yol/metot/şema iskeleti. Kod yazılmadan önce. Frontend bu dosyayla paralel başlayabilir.
1. **İskelet** — bölüm 12.1'deki dizin yapısı, Python 3.14 (`.python-version`), Django 6.1, `uv` bağımlılıkları, ortama göre bölünmüş ayarlar, ruff + mypy + pytest, Docker Compose (Postgres + MinIO), `.env.example`.
2. **Özel kullanıcı modeli + `AUTH_USER_MODEL`** — ilk migration'dan önce. Bu adımı atlamak sonradan tüm veritabanını taşımak demek.
3. **Ortak soyut modeller** — `TimeStampedModel`, `SoftDeleteModel`, `CompanyOwnedModel`, tenant manager, contextvar middleware.
4. **Tüm modeller** — bölüm 5, `TextChoices` enum'ları, index'ler, `CheckConstraint`, partial unique index'ler. İlk migration üretilir ve **elle gözden geçirilir**.
5. **Auth** — kayıt, giriş, refresh, çıkış, şifre sıfırlama, e-posta doğrulama, cookie ayarları.
6. **Yetki katmanı** — permission sınıfları, yetki matrisi, çapraz firma erişim testleri. **Bu adım tamamlanmadan iş modüllerine geçmeyin.**
7. **Hata formatı + exception handler + hata kodu enum'u** — `core/error_codes.py`, `X-Request-ID` middleware, standart yanıt başlıkları.
7b. **Sürümleme iskeleti** — `URLPathVersioning`, `apps/<app>/api/v1/` dizin düzeni, `/health` ve `/ready` (sürümsüz), `/schema/` ve `/docs/` (üretimde kısıtlı).
8. **Adres app'i** — yükleme komutu, normalize fonksiyonu, trigram index, arama endpoint'leri.
9. **Companies + users + invitations** endpoint'leri, e-posta şablonları, `locale/tr`.
10. **Customers + customer contacts.**
11. **Properties** (complex + building).
12. **Elevators** — tam künye, QR token üretimi.
13. **Contracts** — CRUD + asansör ilişkilendirme + aktif sözleşme kısıtı.
14. **Attachments** — Cloudflare R2 (dev: MinIO), imzalı yükleme/indirme URL'i, bucket CORS ayarı, MIME/boyut doğrulaması, `storage_backend` alanı.
15. **QR etiket PDF üretimi.**
16. **Audit log** — sinyaller, hassas alan maskeleme.
17. **Sözleşme doğrulama CI'ı** — `schemathesis` ile yanıt doğrulama, route ↔ spec eşleşme testi, hata kodu enum ↔ spec eşleşme testi, `oasdiff` ile kırıcı değişiklik tespiti, `CHANGELOG-API.md`.
18. **Örnek veri komutu** — 1 firma, 5 kullanıcı, 10 müşteri, 25 bina, 60 asansör, 8 sözleşme.

### Frontend deposu

19. **İskelet** — Next.js, Tailwind, shadcn, next-intl, `tr.json` iskeleti, Türkçe-dize lint kuralı.
20. **API katmanı** — `openapi.json` → `generated.ts`, fetch sarmalayıcı, token refresh, hata kodu → çeviri eşlemesi.
21. **Auth ekranları** — giriş, kayıt, davet kabul, şifre sıfırlama.
22. **Ana yerleşim** — kenar çubuğu, rol bazlı menü, boş durum bileşenleri.
23. **Adres seçici bileşeni** — il/ilçe dropdown + mahalle typeahead + harita, iki yönlü.
24. **Her modül için liste + form ekranları** — müşteri, bina, asansör, sözleşme, kullanıcı.
25. **QR etiket yazdırma akışı.**

Frontend'de bu aşamada işlevsellik hedeflenir; **görsel tasarım ayrı fazda yapılacak.**

---

## 18. Yapılmaması gerekenler

### Dil ve yapı
- ❌ Kodda, tablo adında, enum değerinde, API alanında Türkçe kullanmak
- ❌ Arayüzde İngilizce metin bırakmak
- ❌ JSX/Python dosyasına doğrudan Türkçe dize yazmak
- ❌ Backend'den kullanıcıya gösterilecek Türkçe `message` döndürmek (kod dönün)
- ❌ Enum'ların Türkçe etiketini veritabanında saklamak
- ❌ Frontend derlemesini çalışan backend'e bağımlı kılmak
- ❌ `services.py` veya modelleri sürümlemek (yalnızca API katmanı sürümlenir)
- ❌ Kırıcı değişikliği v1 içinde yapmak
- ❌ Aynı anda ikiden fazla API sürümü canlı tutmak
- ❌ Kapatılmış sürüme 404 dönmek (410 Gone olmalı)
- ❌ Sürümü başlıkta veya query parametresinde taşımak
- ❌ Durum geçişini (`terminate`, `renew`) düz `PATCH` ile yaptırmak
- ❌ Reverse geocoding'i frontend'den doğrudan sağlayıcıya çağırmak
- ❌ Dosya baytlarını uygulama sunucusundan geçirmek
- ❌ Üretimde `/docs/` uçunu açık bırakmak
- ❌ `openapi/v1.yaml` güncellemeden endpoint değiştirmek
- ❌ Sözleşme doğrulama CI'ını kurmadan spec-first çalışmak
- ❌ R2 bucket'ını public yapmak veya public custom domain bağlamak
- ❌ Yerel geliştirmede gerçek R2 bucket'ına yazmak
- ❌ Kişisel veri içeren belgeleri yurt dışı depolamaya KVKK değerlendirmesi yapmadan koymak
- ❌ Django 6.2 LTS çıktığında 6.1'de kalmak
- ❌ Faz 2'de Celery kurmak (Django 6 Tasks çerçevesi varken)
- ❌ `generated.ts` dosyasını elle düzenlemek
- ❌ Frontend ve backend'i farklı üst alan adlarına dağıtmak (cookie kırılır)

### Veri modeli
- ❌ `company_id`'yi request body veya query'den almak
- ❌ Adresi tek serbest metin alanında tutmak
- ❌ Sözleşmeyi binaya bağlamak (müşteriye bağlanır)
- ❌ Bir asansörü aynı anda iki aktif sözleşmeye eklemek
- ❌ Hard delete kullanmak
- ❌ Parayı `float` ile tutmak
- ❌ Sıralı integer ID'leri URL'de göstermek
- ❌ QR token'ı asansör ID'sinden türetmek
- ❌ Dosyaları veritabanında BLOB olarak saklamak
- ❌ Zorunlu alan sayısını abartmak — saha ekibi eksik bilgiyle kayıt açmak zorunda kalır

### Django'ya özgü
- ❌ `ModelSerializer`'da `fields = "__all__"`
- ❌ `uuid.uuid4()` ile birincil anahtar
- ❌ `on_delete=models.CASCADE`
- ❌ `QuerySet.delete()` / `update()` ile iş kaydı değiştirmek
- ❌ Tenant bilgisini thread-local'de tutmak
- ❌ `AUTH_USER_MODEL`'i ilk migration'dan sonra ayarlamaya çalışmak
- ❌ Django admin'i ürün arayüzü yapmak veya firma kullanıcılarına açmak
- ❌ Adres verisini data migration olarak yazmak
- ❌ Türkçe metni doğrudan `.lower()` ile normalize etmek
- ❌ `GenericForeignKey` kullanmak
- ❌ `settings.py`'ı tek dosyada bırakmak
- ❌ N+1 sorgu bırakmak
- ❌ Testleri SQLite'a karşı çalıştırmak
- ❌ Faz 1'de Celery, Redis, async view kurmak

---

## 19. Kabul kriterleri

- [ ] İki farklı firma hesabı birbirinin verisini **hiçbir şekilde** göremiyor (test ile kanıtlanmış)
- [ ] Yönetici çalışan davet ediyor; çalışan e-postadaki linkle kendi şifresini belirleyip giriş yapıyor
- [ ] Her rol için yetki matrisi test ediliyor; yetkisiz istekler 403, tenant ihlali 404 dönüyor
- [ ] 81 il, tüm ilçeler ve mahalleler yüklü; "sisli" araması "Şişli"yi buluyor
- [ ] Haritadan pin bırakıldığında il/ilçe/mahalle otomatik doluyor ve düzeltilebiliyor
- [ ] Bir site altında 3 bina, bir binada 2 asansör kaydı yapılabiliyor
- [ ] Asansörü olan bina silinmeye çalışıldığında 409 + `RECORD_IN_USE` dönüyor, arayüzde Türkçe mesaj görünüyor
- [ ] Bir asansör iki aktif sözleşmeye eklenemiyor (veritabanı seviyesinde engelli)
- [ ] Sözleşme feshedildiğinde asansör ilişkisi silinmiyor, `removed_at` doluyor
- [ ] Bir binanın tüm asansörleri için tek PDF'te QR etiketleri basılıyor, etiket metinleri Türkçe
- [ ] QR okutulunca doğru asansöre gidiliyor; başka firmanın QR'ı 404
- [ ] Her yazma işlemi `audit_log`'a düşüyor; hassas alanlar maskeli
- [ ] Silinen hiçbir kayıt veritabanından fiziksel olarak kaybolmuyor
- [ ] Backend `.py` dosyalarında Türkçe karakter yok (lint geçiyor)
- [ ] Frontend `src/` altında Türkçe dize yok (lint geçiyor)
- [ ] `openapi.json` güncel; `generated.ts` ondan üretilmiş; ikisi de commit edilmiş
- [ ] `/api/v2/elevators` isteği 404 dönüyor (tanımsız sürüme sessizce v1 servis edilmiyor)
- [ ] Her yanıtta `X-Request-ID` ve `X-API-Version` başlıkları var; 500 yanıtının gövdesinde `request_id` görünüyor
- [ ] `schemathesis` spec'teki tüm uçları gerçek API'ye karşı doğruluyor ve geçiyor
- [ ] Spec'te olmayan bir route veya route'u olmayan bir spec girdisi CI'ı kırıyor
- [ ] `core/error_codes.py` içindeki her kod `openapi/v1.yaml` içinde de tanımlı
- [ ] Çeviri dosyasında olmayan bir enum değeri geldiğinde arayüz çökmüyor, ham değeri gösteriyor
- [ ] Aynı `Idempotency-Key` ile iki kez sözleşme oluşturma isteği tek kayıt üretiyor
- [ ] Dosya baytları Django üzerinden geçmiyor — yükleme imzalı URL ile doğrudan R2'ye yapılıyor
- [ ] R2 bucket private; imzalı URL süresi dolduktan sonra erişim reddediliyor
- [ ] Backend Python 3.14 ve Django 6.1 üzerinde çalışıyor; `python -Wd manage.py check` uyarısız
- [ ] `ruff check`, `mypy`, `pytest` hatasız geçiyor
- [ ] Temiz veritabanında: `migrate` → `load_address_data` → `seed_demo_data` baştan sona çalışıyor

---

## 20. Tasarım fazına devir notları

- **Mobil öncelikli düşünün** — Faz 2'de teknisyen bu ekranları telefonda kullanacak.
- **Liste ekranları yoğun veri gösterecek** — 500+ asansörlü firmalar olacak. Tablo tasarımı sıkışık ve taranabilir olmalı; kart yerleşimi uygun değil.
- **Asansör formu uzun** — sekmeli veya adım adım (kimlik / teknik / üretim / kontrol) yapı gerekli. Tek uzun form kullanılabilir değil.
- **Etiket rengi renk kodlu gösterilmeli** ama sadece renge güvenmeyin — metin etiketi de olsun (erişilebilirlik).
- **Boş durum ekranları** önemli: yeni firma sisteme girdiğinde 5 boş liste görecek. Her boş liste bir sonraki adımı önermeli.
- **Türkçe metinler İngilizce'den ortalama %20 uzundur** — buton ve etiket genişliklerini buna göre tasarlayın. "Save" 4 karakter, "Kaydet" 6; "Delete" 6, "Sil" 3 ama "Kalıcı olarak sil" 17.
