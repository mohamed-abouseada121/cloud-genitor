<div dir="rtl" align="right">

# ☁️ Cloud Resource Janitor

**أداة سطح مكتب لفحص وتنظيف الموارد السحابية غير المستخدمة عبر خمسة مزودين سحابيين.**

تدعم الأداة اكتشاف الموارد اليتيمة (Orphaned Resources) مثل الأقراص غير المرتبطة وعناوين IP الحرة، وحذفها بشكل آمن مع الحفاظ على ترتيب التبعيات تلقائيًا.

---

## 🌐 المزودون المدعومون

| المزود | الموارد المدعومة |
|--------|-----------------|
| **AWS** | VPC، Subnet، EC2، EBS، EIP، Security Group، NAT Gateway، Internet Gateway، Route Table |
| **Azure** | VNET، Subnet، VM، Disk، NIC، Public IP، NSG، Resource Group |
| **Alibaba Cloud** | VPC، VSwitch، ECS، Disk، EIP، Security Group، NAT Gateway |
| **GCP** | VPC، Subnet، Compute Instance، Disk، Firewall Rule |
| **Oracle Cloud** | VCN، Subnet، Compute Instance، Block Volume، Public IP، NSG |

---

## ✨ المميزات الرئيسية

- **فحص شامل (Comprehensive Scan):** يكتشف الموارد اليتيمة فقط — الأقراص غير المرتبطة وعناوين IP الحرة
- **فحص هرمي (Hierarchical Scan):** يعرض جميع الموارد على شكل شجرة مرتبة حسب التبعيات
- **حذف آمن بترتيب الطبقات:** يحذف الموارد الداخلية أولاً (الأجهزة الافتراضية) ثم الخارجية (الشبكات) تلقائيًا
- **وضع التجربة (Dry Run):** محاكاة الحذف بدون تنفيذ فعلي على السحابة
- **سجل تدقيق كامل (Audit Log):** يسجل كل عملية حذف في قاعدة بيانات SQLite محلية
- **إعادة المحاولة (Retry):** إعادة محاولة العمليات الفاشلة بضغطة زر
- **تقارير CSV و PDF:** تصدير نتائج الفحص كملفات تقارير
- **دعم حسابات متعددة:** إدارة أكثر من حساب لكل مزود سحابي
- **جدولة الفحص التلقائي:** إمكانية جدولة فحوصات دورية
- **واجهة داكنة/فاتحة:** دعم الوضع الداكن والفاتح

---

## 📁 هيكل المشروع

</div>

```
cloud-genitor/
├── main.py                  # نقطة الدخول الرئيسية
├── requirements.txt         # المكتبات المطلوبة
├── settings.json            # إعدادات التطبيق
├── cleanup_state.db         # قاعدة بيانات سجل العمليات
│
├── config/
│   ├── settings.py          # تحميل وحفظ الإعدادات
│   └── scheduler.py         # جدولة الفحص التلقائي
│
├── models/
│   └── resource.py          # نموذج البيانات الموحد للموارد السحابية
│
├── providers/
│   ├── base_provider.py     # الواجهة الأساسية لكل المزودين
│   ├── account_manager.py   # إدارة الحسابات المتعددة
│   ├── aws_manager.py       # مزود AWS
│   ├── azure_manager.py     # مزود Azure
│   ├── alibaba_manager.py   # مزود Alibaba Cloud
│   ├── gcp_manager.py       # مزود GCP
│   └── oracle_manager.py    # مزود Oracle Cloud
│
├── workers/
│   ├── base_worker.py       # القاعدة المشتركة للعمليات الخلفية
│   ├── scan_worker.py       # عامل الفحص في الخلفية
│   ├── delete_worker.py     # عامل الحذف في الخلفية
│   └── snapshot_worker.py   # عامل النسخ الاحتياطي
│
├── ui/
│   ├── main_window.py       # النافذة الرئيسية
│   ├── sidebar.py           # الشريط الجانبي لاختيار المزود
│   ├── resource_tree.py     # شجرة عرض الموارد
│   ├── filter_bar.py        # شريط الفلترة
│   ├── log_console.py       # وحدة عرض السجلات
│   └── dialogs/             # نوافذ الحوار (تأكيد الحذف، بيانات الاعتماد)
│
├── utils/
│   ├── credentials.py       # إدارة بيانات الاعتماد
│   ├── dependency_graph.py  # ترتيب الحذف حسب التبعيات
│   ├── pagination.py        # التعامل مع الصفحات في API
│   └── report_exporter.py   # تصدير التقارير (CSV / PDF)
│
├── db/
│   └── state_manager.py     # إدارة قاعدة بيانات SQLite
│
└── plugins/                 # إضافات مزودين خارجيين (اختياري)
```

<div dir="rtl" align="right">

---

## 🚀 التثبيت والتشغيل

### المتطلبات الأساسية

- نظام تشغيل Linux أو macOS أو Windows
- Python 3.10 أو أحدث
- واجهة رسومية (X11 أو Wayland)

### خطوات التثبيت

**1. استنساخ المستودع:**

</div>

```bash
git clone https://github.com/your-username/cloud-genitor.git
cd cloud-genitor/cloud-genitor
```

<div dir="rtl" align="right">

**2. إنشاء بيئة افتراضية وتثبيت المكتبات:**

</div>

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

<div dir="rtl" align="right">

**3. تشغيل التطبيق:**

</div>

```bash
./venv/bin/python main.py
```

<div dir="rtl" align="right">

أو بتفعيل البيئة الافتراضية أولاً:

</div>

```bash
source venv/bin/activate
python main.py
```

<div dir="rtl" align="right">

---

## 🔑 إعداد بيانات الاعتماد

### AWS

يمكنك إعداد بيانات AWS بإحدى الطريقتين:
- **عبر الملف:** إعداد `~/.aws/credentials` باستخدام `aws configure`
- **عبر التطبيق:** اضغط على زر 🔑 **Credentials** وأدخل الـ Access Key و Secret Key

### Azure

قم بتعيين متغيرات البيئة التالية أو أدخلها عبر التطبيق:

</div>

```bash
export AZURE_TENANT_ID="..."
export AZURE_CLIENT_ID="..."
export AZURE_CLIENT_SECRET="..."
```

<div dir="rtl" align="right">

### Alibaba Cloud

أدخل الـ Access Key ID و Access Key Secret عبر نافذة **Credentials** في التطبيق.

### GCP

- استخدم Application Default Credentials: `gcloud auth application-default login`
- أو حدد مسار ملف Service Account JSON عبر التطبيق

### Oracle Cloud

تأكد من وجود ملف الإعدادات في `~/.oci/config` باستخدام `oci setup config`.

---

## 📖 طريقة الاستخدام

### 1. اختيار المزود السحابي

من الشريط الجانبي على اليسار، اضغط على اسم المزود (AWS، Azure، Alibaba، إلخ).

### 2. إعداد بيانات الاعتماد

اضغط على زر 🔑 **Credentials** في شريط الأدوات وأدخل بيانات حسابك.

### 3. اختيار المنطقة ونوع الفحص

- **المنطقة (Region):** اختر منطقة محددة أو "All Regions" لفحص جميع المناطق
- **نوع الفحص:**
  - **Comprehensive (Orphans):** يعرض فقط الموارد اليتيمة غير المستخدمة
  - **Hierarchical (Full Tree):** يعرض جميع الموارد مرتبة هرميًا

### 4. تشغيل الفحص

اضغط على زر 🔍 **Scan** أو اضغط `Ctrl+S`.

### 5. مراجعة النتائج

ستظهر الموارد في شجرة تفاعلية تعرض:
- اسم المورد ونوعه
- معرف المورد (Resource ID)
- المنطقة والحالة
- التكلفة الشهرية المقدرة
- طبقة الحذف (Layer)

### 6. تحديد الموارد للحذف

ضع علامة ✓ بجانب الموارد التي تريد حذفها. سيظهر في الأعلى التوفير الشهري المقدر.

### 7. الحذف

- **وضع التجربة (Dry Run):** فعّل خيار "Dry Run" لمحاكاة الحذف بدون تنفيذ فعلي
- **الحذف الفعلي:** ألغِ تفعيل "Dry Run" ثم اضغط 🗑 **Delete Selected** أو `Ctrl+D`
- ستظهر نافذة تأكيد قبل التنفيذ

### 8. تصدير التقارير

اضغط على 📄 **Export** أو `Ctrl+E` لتصدير النتائج كملفات CSV و PDF.

---

## ⌨️ اختصارات لوحة المفاتيح

| الاختصار | الوظيفة |
|----------|---------|
| `Ctrl+S` | بدء الفحص |
| `Ctrl+D` | حذف الموارد المحددة |
| `Ctrl+E` | تصدير التقارير |
| `Ctrl+L` | مسح سجل وحدة التحكم |
| `Ctrl+Z` | إلغاء التحديد |
| `F5` | تحديث قائمة المناطق |

---

## 🔄 طبقات الحذف الآمن

يستخدم التطبيق نظام طبقات لضمان حذف الموارد بالترتيب الصحيح:

| الطبقة | النوع | الأمثلة |
|--------|-------|---------|
| 1 | الحوسبة وقواعد البيانات | EC2، VM، ECS، Compute Instance |
| 2 | الموارد المرتبطة | الأقراص، عناوين IP، واجهات الشبكة |
| 3 | بوابات الشبكة | NAT Gateway، Internet Gateway |
| 4 | مستوى الشبكة الفرعية | Subnet، Security Group، Route Table |
| 5 | الحاوية الخارجية | VPC، VNET، VCN |

يتم الحذف من الطبقة 1 (الداخلية) إلى الطبقة 5 (الخارجية) مع انتظار 5 ثوانٍ بين كل طبقة لضمان اكتمال الحذف.

---

## 🗄️ قاعدة بيانات التدقيق

يحتفظ التطبيق بقاعدة بيانات SQLite محلية (`cleanup_state.db`) تسجل:

- **سجل الحذف:** كل عملية حذف مع الحالة (نجاح / فشل / معلق / محظور)
- **سجل الفحص:** تاريخ كل عملية فحص مع عدد الموارد المكتشفة

في حالة إغلاق التطبيق بشكل غير متوقع، سيكتشف العمليات المعلقة عند إعادة التشغيل ويعرض خيار تحديثها.

---

## 🔌 نظام الإضافات

يدعم التطبيق إضافة مزودين سحابيين جدد عبر مجلد `plugins/`. لإنشاء إضافة جديدة:

1. أنشئ ملف Python جديد في مجلد `plugins/`
2. اكتب كلاس يرث من `CloudProvider`
3. طبّق الدوال المطلوبة: `connect`، `list_regions`، `scan_comprehensive`، `scan_hierarchical`، `delete_resource`

سيتم اكتشاف الإضافة تلقائيًا عند تشغيل التطبيق.

---

## 📝 ملاحظات هامة

> ⚠️ **تحذير:** عمليات الحذف لا يمكن التراجع عنها. تأكد من مراجعة الموارد المحددة بعناية قبل التنفيذ.

> 💡 **نصيحة:** استخدم وضع **Dry Run** دائمًا في المرة الأولى للتأكد من صحة النتائج قبل الحذف الفعلي.

> 🔒 **الأمان:** لا يتم تخزين كلمات المرور أو المفاتيح السرية بشكل دائم — يتم استخدام ملفات الإعدادات المحلية وملفات التوثيق الرسمية لكل مزود.

---

## 📄 الرخصة

هذا المشروع مرخص تحت رخصة MIT.

</div>
