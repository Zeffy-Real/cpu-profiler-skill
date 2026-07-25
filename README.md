# Linux 持续 CPU Profiling 工具

> 让 perf 像黑匣子一样 7x24 常驻后台运行，出问题时只需指定时间点就能调出当时的 CPU 采样数据，生成火焰图定位根因。

## 简介

本工具是一个 Linux 持续 CPU Profiling 采集系统，通过将 `perf record` 封装为后台常驻守护进程，实现 7x24 小时不间断的 CPU 采样数据采集。当系统出现性能问题时，只需指定时间点或时间范围，即可快速查询并生成火焰图，帮助开发人员定位根因。

**解决的核心问题**：传统 perf 使用方式是"先出问题再采集"，但很多性能问题是瞬时发生的，等手动启动 perf 时问题已经消失。本工具将采集前置为持续运行，实现"事后回溯"的能力。

## 架构设计

### 两层解耦架构

```
┌─────────────────────── 持续采集层 ───────────────────────┐
│  ProfilerDaemon (systemd)                                │
│  ├── perf record -F 99 -a -g -o perf.data.YYYYMMDD_HHMMSS│
│  │   -- sleep 30  (30秒切片，自动结束)                    │
│  ├── FileRotator (轮转/过期清理/磁盘检查/索引管理)         │
│  └── 信号处理 (SIGTERM 优雅退出)                          │
└──────────────────────────┬───────────────────────────────┘
                           │ 文件系统 + index.json
                           ▼
┌─────────────────────── 按需查询层 ───────────────────────┐
│  FastAPI (uvicorn, systemd)                              │
│  ├── GET  /api/v1/health          → 健康检查             │
│  ├── GET  /api/v1/profile/slices  → 切片列表             │
│  ├── GET  /api/v1/profile/flamegraph?time= → 单点火焰图   │
│  └── POST /api/v1/profile/flamegraph       → 范围火焰图   │
│                                                          │
│  FlameGraphGenerator                                     │
│  ├── perf script -i perf.data > perf.script              │
│  ├── stackcollapse-perf.pl < perf.script > folded.txt    │
│  └── flamegraph.pl < folded.txt > flamegraph.svg         │
└──────────────────────────────────────────────────────────┘
```

### 数据流

```
采集流:  perf record → perf.data.YYYYMMDD_HHMMSS → index.json
查询流:  HTTP请求 → 查找index.json → 定位perf.data → 生成SVG → 返回
```

## 核心特性

| 特性 | 说明 |
|------|------|
| 持续采集 | 7x24小时不间断perf record，30秒切片自动轮转 |
| 自动轮转 | 按时间切片生成独立文件，避免单文件过大 |
| 按需查询 | 指定时间点即可调出对应CPU采样数据 |
| 火焰图生成 | 集成Brendan Gregg FlameGraph工具链 |
| systemd管理 | 崩溃自愈(Restart=always)，开机自启 |
| 低开销 | 99Hz采样，CPU开销仅1-3% |
| 磁盘管理 | 自动过期清理(默认2h)，磁盘空间预检 |
| 可配置 | 全部参数通过环境变量配置 |
| 范围合并 | 支持时间范围查询，合并多个切片生成火焰图 |
| 自愈能力 | perf失败自动重试(3次)，systemd自动重启 |

## 快速开始

### 环境要求

| 组件 | 版本要求 | 说明 |
|------|---------|------|
| Linux Kernel | >= 4.9 | perf基础功能支持 |
| Python | >= 3.10 | 类型注解和dataclass支持 |
| perf | >= 6.0 | 随linux-tools包安装 |
| FlameGraph | 最新版 | flamegraph.pl + stackcollapse-perf.pl |
| pip | >= 21.0 | Python包管理 |

### FlameGraph 工具安装

```bash
git clone https://github.com/brendangregg/FlameGraph.git ~/FlameGraph
sudo cp ~/FlameGraph/flamegraph.pl ~/FlameGraph/stackcollapse-perf.pl /usr/local/bin/
sudo chmod +x /usr/local/bin/flamegraph.pl /usr/local/bin/stackcollapse-perf.pl
```

### perf 权限配置

```bash
# 临时设置（重启失效）
sudo sh -c 'echo -1 > /proc/sys/kernel/perf_event_paranoid'

# 永久设置
echo 'kernel.perf_event_paranoid = -1' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### 一键安装

```bash
git clone <repository_url>
cd cpu-profiler-skill
sudo bash install.sh
```

### 验证

```bash
# 启动服务
sudo systemctl start cpu-profiler-collector cpu-profiler-api

# 检查健康状态
curl http://localhost:8765/api/v1/health

# 查看切片列表（等待30秒后）
curl http://localhost:8765/api/v1/profile/slices

# 生成火焰图
curl "http://localhost:8765/api/v1/profile/flamegraph?time=$(date +%Y%m%d_%H%M%S)" > flamegraph.svg
```

## 配置说明

### 环境变量

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `SAMPLE_FREQ` | 99 | 采样频率(Hz)，99避免与100Hz定时器锁步 |
| `SLICE_DURATION` | 30 | 切片时长(秒)，单文件5-15MB |
| `RETENTION_HOURS` | 2 | 数据保留时长(小时) |
| `DATA_DIR` | /var/lib/cpu-profiler | 数据存储目录 |
| `API_HOST` | 0.0.0.0 | API监听地址 |
| `API_PORT` | 8765 | API监听端口 |
| `PYTHONUNBUFFERED` | 1 | Python输出不缓冲(systemd用) |

### 磁盘容量估算

| 采样频率 | 切片时长 | 单文件大小 | 1小时数据 | 2小时保留 |
|---------|---------|-----------|----------|----------|
| 99Hz | 30s | 5-15MB | 600-1800MB | 1.2-3.6GB |
| 49Hz | 30s | 3-8MB | 360-960MB | 720MB-1.9GB |
| 199Hz | 30s | 10-30MB | 1.2-3.6GB | 2.4-7.2GB |

### 自定义配置示例

```bash
# systemd 自定义配置
# 编辑 /etc/systemd/system/cpu-profiler-collector.service
# 修改 Environment 行：
Environment=SAMPLE_FREQ=199
Environment=SLICE_DURATION=60
Environment=RETENTION_HOURS=4
Environment=DATA_DIR=/data/cpu-profiler

# 重载并重启
sudo systemctl daemon-reload
sudo systemctl restart cpu-profiler-collector
```

## API 文档

### GET /api/v1/health

健康检查端点。

**请求参数**：无

**响应**：
```json
{
  "status": "ok",
  "collector_running": true,
  "data_dir": "/var/lib/cpu-profiler",
  "disk_usage_mb": 45.2,
  "slice_count": 120
}
```

**状态码**：200 (成功)

---

### GET /api/v1/profile/slices

获取切片列表，支持时间范围过滤。

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| start | string | 否 | 开始时间 YYYYMMDD_HHMMSS |
| end | string | 否 | 结束时间 YYYYMMDD_HHMMSS |

**响应**：
```json
{
  "slices": [
    {
      "timestamp": "20260724_153045",
      "file": "/var/lib/cpu-profiler/perf.data.20260724_153045",
      "duration": 30,
      "size_bytes": 5242880,
      "status": "success"
    }
  ],
  "total_count": 1
}
```

**状态码**：200 (成功) | 400 (参数错误)

---

### GET /api/v1/profile/flamegraph

单点火焰图生成。根据指定时间查找对应的切片并生成火焰图。

**查询参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| time | string | 是 | - | 目标时间 YYYYMMDD_HHMMSS |
| duration | int | 否 | 30 | 搜索窗口(秒) |
| width | int | 否 | 1200 | SVG宽度(像素) |
| height | int | 否 | 16 | 帧高度(像素) |
| title | string | 否 | CPU Flame Graph | 图表标题 |

**响应**：SVG图像 (`image/svg+xml`)

**状态码**：200 (成功) | 400 (参数错误) | 404 (未找到) | 500 (生成失败)

---

### POST /api/v1/profile/flamegraph

范围火焰图生成。合并指定时间范围内的所有切片，生成聚合火焰图。

**请求体**：
```json
{
  "start_time": "20260724_153000",
  "end_time": "20260724_154000",
  "width": 1200,
  "height": 16,
  "title": "CPU Flame Graph"
}
```

**响应**：SVG图像 (`image/svg+xml`)

**状态码**：200 (成功) | 400 (参数错误) | 404 (无数据) | 500 (生成失败)

## 测试

### 运行测试

```bash
cd cpu-profiler-skill
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pytest -v --tb=short
```

### 测试覆盖

| 测试文件 | 测试数 | 覆盖内容 |
|---------|--------|---------|
| test_collector.py | 26 | FileRotator(16) + ProfilerDaemon(10) |
| test_api.py | 10 | Health(1) + Slices(3) + Flamegraph(6) |
| test_flamegraph.py | 9 | Generation(4) + Unit(5) |
| **合计** | **45** | 全模块覆盖 |

### 测试策略

- **纯单元测试**：不依赖外部工具，任何环境可运行
- **集成测试**：依赖perf/flamegraph，通过fixture自动skip
- **API测试**：使用FastAPI TestClient，无需启动服务器
- **测试隔离**：每个测试使用独立临时目录，实例级stop_requested

## 项目结构

```
cpu-profiler-skill/
├── src/
│   ├── __init__.py              # 包初始化
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py            # Config dataclass + from_env()
│   │   └── flamegraph.py        # FlameGraphGenerator 管线
│   ├── collector/
│   │   ├── __init__.py
│   │   ├── daemon.py            # ProfilerDaemon 守护进程
│   │   └── rotator.py           # FileRotator 文件轮转
│   └── api/
│       ├── __init__.py
│       ├── models.py            # Pydantic 数据模型
│       └── server.py            # FastAPI 应用
├── systemd/
│   ├── cpu-profiler-collector.service  # 采集服务
│   └── cpu-profiler-api.service       # API服务
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # pytest fixtures
│   ├── test_collector.py        # 采集模块测试
│   ├── test_api.py              # API测试
│   └── test_flamegraph.py       # 火焰图测试
├── docs/
│   └── design.md                # 设计文档
├── .gitignore
├── README.md
├── install.sh                   # 一键安装脚本
├── pytest.ini
├── requirements.txt
└── skill.yaml                   # Skill配置
```

## 运维操作

### 服务管理

```bash
# 启动服务
sudo systemctl start cpu-profiler-collector cpu-profiler-api

# 停止服务
sudo systemctl stop cpu-profiler-collector cpu-profiler-api

# 重启服务
sudo systemctl restart cpu-profiler-collector cpu-profiler-api

# 查看状态
sudo systemctl status cpu-profiler-collector cpu-profiler-api

# 查看日志
sudo journalctl -u cpu-profiler-collector -f
sudo journalctl -u cpu-profiler-api -f
```

### 手动运行

```bash
# 单次采集
python -m src.collector.daemon --once

# 干运行（打印命令不执行）
python -m src.collector.daemon --dry-run

# 启动API服务器
python -m uvicorn src.api.server:app --host 0.0.0.0 --port 8765
```

### 故障排查

| 问题 | 可能原因 | 解决方案 |
|------|---------|---------|
| perf: Operation not permitted | perf_event_paranoid未设置 | `echo -1 > /proc/sys/kernel/perf_event_paranoid` |
| flamegraph.pl not found | FlameGraph未安装 | 运行install.sh或手动安装 |
| 磁盘空间不足 | 数据未清理 | 检查RETENTION_HOURS配置 |
| API返回404 | 切片不存在 | 检查时间格式和切片列表 |
| collector未运行 | systemd服务失败 | `journalctl -u cpu-profiler-collector` |
| Permission denied | 非root运行 | 设置DATA_DIR为可写目录 |

## License

MIT License

Copyright (c) 2026 CPU Profiler Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
