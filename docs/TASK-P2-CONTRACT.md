# TASK-P2 契约:AppConfig 表 + 配置读写 API(神朱 2026-10-15 拍板,方案见 docs/CONTAINER-SETUP-PLAN.md §2)

> Codex 执行。读完本契约与「只读清单」立即写码,禁止浏览式探索仓库。
> 硬约定(AGENTS.md):不新增第三方依赖、测试全 mock 不触网、只 commit 不 push、pytest 基线 1632 只增不减。

## 背景一句话

容器版用户没有 .env,要在 /setup 向导里填 key 存进 SQLite;既有代码 30 处直接读 `os.environ`(且普遍带 `environ=None` 注入参数)——**一处都不改**,用「启动/保存时把 AppConfig 注入 os.environ(env 优先)」打通。

## 只读清单(只准读这 5 个,读完立即写码)

1. `AGENTS.md`(仓库根,写码硬约定)
2. `docs/CONTAINER-SETUP-PLAN.md` §2(配置注入策略,本契约的上游)
3. `backend/db/models.py` 的 `OriginCache` 类与文件头(表定义风格:Mapped/mapped_column/utcnow/唯一键命名 uq_*)
4. `backend/app/api/collections.py`(路由风格:裸 JSON Body + 400 中文报错 + `_optional_text()` 处理单测直调 FieldInfo + `Depends(get_session)`)
5. `backend/services/intro.py` 的 `resolve_provider()` 与 `describe_llm()`(env 解析口径 + 「key 不出响应」的掩码先例)

## 落地契约(照此实现,不得自创字段/路由)

### 1. `backend/db/models.py` 追加 `AppConfig`

```python
class AppConfig(Base):
    __tablename__ = "app_config"
    __table_args__ = (UniqueConstraint("key", name="uq_app_config_key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="setup")  # setup|env
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                 default=utcnow, onupdate=utcnow)
```
旧库靠 create_all 自动建表,不进 COLUMN_MIGRATIONS(新表无存量)。

### 2. 新建 `backend/services/app_config.py`

- **白名单 SCHEMA**(唯一可写入口,防 /api/config 变任意 env 注入面)。每键一个 `ConfigField` dataclass:`key(env 名), label(中文), hint(中文,含坑文案), required(bool), secret(bool), needs_restart(bool), group("amap"|"llm")`。恰好 7 键:
  - `WHERE2GO_AMAP_KEY`(高德 Web服务 key,required,secret,group=amap)
  - `WHERE2GO_AMAP_JS_KEY`(高德 Web端 JSAPI key,required,secret,group=amap,hint 写明与 Web服务 key 平台隔离、混用报 USERKEY_PLAT_NOMATCH)
  - `WHERE2GO_AMAP_SECURITY_JS_CODE`(JS 安全密钥,required,secret,group=amap,hint 写明缺失报 INVALID_USER_SCODE)
  - `WHERE2GO_LLM_PROVIDER`(可选,非 secret,group=llm)
  - `WHERE2GO_LLM_API_KEY`(可选,secret,group=llm)
  - `WHERE2GO_LLM_BASE_URL`(可选,非 secret,group=llm)
  - `WHERE2GO_LLM_MODEL`(可选,非 secret,group=llm)
- `def iter_fields() -> tuple[ConfigField, ...]`、`def is_allowed(key) -> bool`。
- `def load_into_environ(session) -> list[str]`:读 app_config 全表,**只对 os.environ 里没有(或为空串)的键**注入;返回注入的键名列表。容器 env 永远压过库配置。
- `def set_key(session, key, value) -> None`:白名单外抛 `ValueError("不允许配置的配置项:…")`;值 strip;空串=删除该行(同时 `os.environ.pop(key, None)`?——**不 pop**:env 是容器给的,库里删行只代表「向导配置已清除」,os.environ 同步置空串即可,与 load 的「空串视为未设」口径一致);非空= upsert 行(source="setup")+ 同步 `os.environ[key]=value`(进程内热生效,intro/amap 都是每次调用现读 env)。
- `def mask_value(value: str) -> str`:空→`""`;len≤4→`"****"`;否则前 2 + `"…"` + 后 2(如 `ab…yz`)。secret 键的 GET 响应只回「已配置(掩码)」或「未配置」;**完整值永不进 API 响应/日志**(照 intro.describe_llm 口径)。
- `def effective_source(session, key) -> str`:返回来源标记——os.environ 该键非空 → `"env"`;否则查库行 value 非空 → `"setup"`;都空 → `""`。配套 `def is_configured(session, key) -> bool`(= effective_source 非空)。
- `def missing_required(session) -> list[str]`:required 且 effective 为空的键名列表(给 /api/health 与向导横幅)。

### 3. 新建 `backend/app/api/config.py` + `app/main.py` 挂路由

- `GET /api/config` → `{"items":[{key,label,hint,required,secret,group,configured(bool),source,masked(仅 secret 且 configured 时给掩码)}], "missing_required":[...], "note":"配置保存在容器数据卷的数据库中;环境变量优先于向导配置。key 不回显明文。"}`。**响应里 grep 不到任何明文 key 值**(测试断言)。
- `POST /api/config` 裸 JSON `{"key": "...", "value": "..."}`:白名单外 → 400 中文;成功 → upsert + 热生效,响应 `{"ok":true,"key":...,"configured":true,"source":"setup","note":"已保存并即时生效(无需重启)"}`(needs_restart 键目前 7 个里**没有**,字段留着但恒 false)。也接受批量 `{"items":[{key,value},...]}`?——**不做**,单键提交,向导逐个保存。
- `GET /api/health` → 200 `{"status":"ok"|"missing_config", "missing_required":[...], "version": "0.1.0-beta"}`(缺必填不报 5xx:服务可用但功能降级,给横幅/向导用)。
- 路由风格照 collections.py:`_optional_text()` 兼容单测直调 FieldInfo、400 中文报错。
- `main.py`:import config 模块、include_router(prefix="/api")、加 lifespan(startup 时 `with open_session() as s: app_config.load_into_environ(s)`,用 `lifespan` 参数挂到 FastAPI,**不用已弃用的 on_event**;注意 StaticFiles mount 在 include_router 之后不变的既有顺序)。

### 4. 新建 `backend/test_app_config.py`(≥25 用例,全 mock 不触网)

覆盖:表结构与唯一键;set_key 白名单外拒;空串删行;os.environ 优先(load_into_environ 不覆盖已有 env);set_key 后 `amap.resolve_key()` 立即拿到新值、`intro.resolve_provider()` 立即生效(直接调函数断言,不起服务);mask_value 各长度边界(空/1/4/5 字符);GET /api/config 响应不含明文(json.dumps 后 assert 明文 not in,照 test_classify.py 928 行先例);missing_required 三态(env 配/库配/都没配);POST 校验 400;lifespan 注入后 env 生效(用 open_session + 临时库);/api/health 形状;单测直调(FieldInfo)兼容。网络兜底:requests.Session.request 换抛错(照 conftest 既有 no_network 套路,若 conftest 无全局兜底则本文件加 autouse fixture)。

## 验收

- `cd backend && ../.venv/bin/python -m pytest -q` 全绿,基线 **1632 passed 零回归**,新增 ≥25。
- 不动那 30 处 env 读取点、不动 data_sources/、不动 index.html。
- 不新增第三方依赖。
- commit 消息带 TASK-P2。只 commit 不 push。

## 版本号口径

`/api/health` 的 version 常量 `"0.1.0-beta"`(神朱拍板决策④:先 beta,NAS 冒烟过再 v1.0.0)。
