# 纸质问卷识别系统 · 新手教程

## 第一步：安装 Python

1. 打开浏览器，访问 **https://www.python.org/downloads/**
2. 点页面上黄色大按钮 **「Download Python 3.x」** 下载安装包
3. 双击运行安装包，**最重要的一步**：在安装界面底部勾选 ✅ **「Add Python to PATH」**（添加到 PATH），然后点 **Install Now**
4. 等待安装完成，点 **Close**

**验证安装**：按 `Win + R`，输入 `cmd` 回车，打开黑色命令行窗口，输入：
```
python --version
```
看到 `Python 3.x.x` 就说明装好了。

---

## 第二步：获取本项目

把整个项目文件夹（`问卷识别系统`）放到电脑上任意位置，例如 `D:\User\Documents\问卷识别系统`。

---

## 第三步：安装项目依赖（只需一次）

1. 打开命令行（`Win + R` → 输入 `cmd` 回车）
2. 进入项目文件夹（**注意换成你自己的路径**）：
```
cd /d D:\User\Documents\问卷识别系统
```
3. 安装所有依赖（复制下面这行，粘贴到命令行，回车）：
```
pip install -r requirements.txt
```
4. 等待安装完成（会下载几个包，约 1-2 分钟）。看到 `Successfully installed ...` 即成功。

> **如果下载很慢**：用国内镜像加速，输入：
> ```
> pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

---

## 第四步：配置大模型（只需一次）

系统需要一个"视觉大模型"来识别问卷图片。推荐用**通义千问**（阿里云免费额度）。

1. 复制配置模板为 `config.yaml`：命令行输入（首次配置才需要）
```
copy config\config.yaml.example config\config.yaml
```
2. 用记事本（或 VS Code）打开 `config\config.yaml` 文件
3. 找到 `llm:` 这一段，修改三处：
```yaml
llm:
  api_key_env: "sk-你的真实key"                                    # ← 填你的通义千问 key
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"   # 通义千问(已填好)
  model: "qwen-vl-max"                                            # 模型(已填好)
```
4. **获取通义千问 key**：访问 https://dashscope.console.aliyun.com/ ，注册登录 → 左侧「API-KEY 管理」→ 创建新的 API-KEY → 复制，填到上面 `api_key_env` 引号里
5. 保存文件

---

## 第五步：放入问卷 PDF

把要识别的问卷 PDF 文件（11 页扫描件）放进 `data\` 文件夹。

> 每份 PDF 是一份被试的问卷。

---

## 第六步：启动系统

1. 命令行进入项目目录（同第三步）：
```
cd /d D:\User\Documents\问卷识别系统
```
2. 启动服务：
```
python -m src.review_server --port 8001
```
3. 看到 `问卷识别核对前端已启动: http://localhost:8001` 后，**不要关闭这个命令行窗口**（关了服务就停了）

---

## 第七步：浏览器打开

打开浏览器（推荐 Chrome / Edge），地址栏输入：

**http://127.0.0.1:8001**

> ⚠️ 必须用 `127.0.0.1`，**不要用 `localhost`**（会连不上）。看到核对页面就成功了。

页面顶部有蓝色导航栏，三个页面可切换：
- 📄 **核对** — 逐页核对识别结果
- 📋 **问卷星导入** — 批量提交到问卷星
- 🔍 **增量扫描** — 识别新放进来的问卷

---

## 第八步：识别问卷

### 首次 / 全量识别
注：做目前的问卷识别可以不管这一部分，因为我已经做过全量识别了，直接新增问卷就行了。
在命令行（**新开一个 cmd**，别关掉第六步那个）输入：
```
cd /d D:\User\Documents\问卷识别系统
python -m src.run batch --dir data --out out/results.csv
```
等待识别完成（每份约 2 分钟，会自动拆页/识别/校验）。

### 后续增量识别（加了新问卷）
在网页点「🔍 增量扫描」→「🔄 扫描」→ 勾选新增的 PDF →「开始增量识别」（自动追加，不用命令行）。

---

## 第九步：核对结果（📄 核对页）

1. 顶部选样本，`←` `→` 切换样本，`↑` `↓` 翻页
2. **左边原图，右边答案**，逐题对照
3. 量表题有**选项条**（如 `1○ 2○ 3● 4○`），点数字快速修正；基本信息直接改输入框
4. 改完点右上「💾 保存修改」（回写到 results.csv）
5. 「📋 日志」可查看识别问题（红色=错误，黄色=警告），点「跳转」直接到对应题
6. **核对完一个样本**：翻到最后一页（第20页）会自动标记「已核对」✓，或手动点「标记已核对」

> 💡 **建议**：开始核对前先点「💾 备份」存个快照，改错了能「↩ 恢复」。

---

## 第十步：导入问卷星（📋 问卷星导入页）

1. 顶部点「📋 问卷星导入」
2. **问卷星链接**：确认/修改成你的问卷星问卷地址（点「保存链接」）
3. **录入人**：填你的缩写（如 `zhangsan`），可勾选加「(自动化)」标记
4. **勾选已核对样本**（绿色✓的才是已核对）
5. 勾「✅ 自动提交」+ **不要勾**「无头模式」（要弹浏览器窗口）
6. 点「🚀 开始导入」
7. 会弹出 Edge 浏览器窗口，自动填写 312 项 → 提交
8. **如果弹出验证码**：在弹出的浏览器窗口手动拖滑块/点验证 → 点提交 → 成功后回网页
9. 看进度条和状态：🟦已提交 / 🟥异常 / ⬜未提交

> ⚠️ 无头可能会被问卷星反爬拦截。

---

## 常见问题

| 问题 | 解决 |
|---|---|
| 命令行报 `python 不是内部命令` | 重装 Python，务必勾「Add to PATH」 |
| 报 `No module named 'yaml'` 等 | 回到第三步，`pip install -r requirements.txt` |
| 浏览器打开空白页/连不上 | 用 `127.0.0.1:8001`（不是 localhost）；按 Ctrl+F5 强制刷新 |
| `Address already in use` 端口占用 | 换端口：`python -m src.review_server --port 8002`，浏览器也改 8002 |
| 识别很慢 | 正常，每份约2分钟（调大模型）；5份约10分钟 |
| 姓名/日期识别错 | 手写 OCR 有误差，在核对页直接改 |
| 问卷星提交失败 | 看弹窗提示；用有头模式；`work\wjx_debug_*.png` 有截图 |
| 通义千问报错 401 | API key 填错了，回第四步检查 |

---

## 首次归纳模板（新问卷类型才需要）

> 如果是**全新的问卷**（题目和示例不一样），需要先做一次归纳（约10分钟）。已有 `config/scales.yaml` 的可跳过。

```
cd /d D:\User\Documents\问卷识别系统
python -m src.run induct --focused --dir data --out config/scales.yaml
```
完成后看 `work/induct/scales_review.html`（浏览器打开）核对量表切分，有问题改 `config/scales.yaml`。模板定型后日常识别不再重跑。

---

## 文件说明（不用改，仅供参考）

| 文件/文件夹 | 作用 |
|---|---|
| `data\` | 放输入的问卷 PDF |
| `out\results.csv` | 识别结果（Excel 可打开） |
| `config\config.yaml` | 大模型配置（第四步改的；从 .example 复制，含密钥不发布） |
| `config\config.yaml.example` | 配置模板（发布自带，复制为 config.yaml 用） |
| `config\scales.yaml` | 量表模板（自动生成，一般不改） |
| `config\wjx_mapping.yaml` | 问卷星字段映射（一般不改） |
| `work\` | 中间文件（识别页图、备份、缓存等，可随时删） |
| `logs\` | 运行日志 |

---

## 日常使用速查（装好后）

```
1. 启动服务：python -m src.review_server --port 8001
2. 浏览器：http://127.0.0.1:8001
3. 新问卷放 data\ → 增量扫描页识别
4. 核对页核对 → 标记已核对
5. 问卷星导入页 → 自动提交
```
