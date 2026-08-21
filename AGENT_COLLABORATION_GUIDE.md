# Plan Agent（高阶智囊与架构顾问）智能体接入指南

本 API 将底层强大的 ChatGPT 深度推理与规划能力封装为标准接口，旨在充当本地执行智能体（Execution Agent / Coder Agent）的**高阶智囊、架构顾问与决策大脑（Plan Agent）**。

---

## 1. 核心定位：按需随时调用的“全能架构顾问”

本地 Agent 与 Plan Agent 的协同是**完全自由、按需、实时的双向互动**，不局限于任何固定流程。只要本地 Agent 需要思考、规划、排查或决策，**随时随地都可以发起调用**：

```
┌────────────────────────────────────────────────────────┐
│               本地执行 Agent (Execution Agent)           │
│  - 掌控本地终端、文件系统、环境依赖、GPU 训练与代码调试        │
└───────────────────────────▲────────────────────────────┘
                            │ 随时按需交互 (自由多轮对话)
                            ▼
┌────────────────────────────────────────────────────────┐
│               Plan Agent (本 API 驱动)                  │
│  - 擅长：顶层规划、算法推导、代码审计、报错诊断、实验决策、方向把控 │
└────────────────────────────────────────────────────────┘
```

### 常见随需调用场景（按需自由触发）
1. **启动阶段**：*“我要做 X 任务，请给我一个清晰的总体架构设计与分步实施 Plan。”*
2. **编码阶段**：*“我写好了这个关键模块，你帮我审一下逻辑有没有潜在 Bug / 数据泄露？”*
3. **遇到报错**：*“本地跑代码报了这个 Traceback / CUDA 错误，帮我分析下原因和修复方案。”*
4. **决策分歧**：*“目前有两个实现思路 A 和 B，从显存开销和收敛速度上哪个更优？”*
5. **实验分析**：*“这是最新的 Loss 曲线和评估指标，当前模型欠拟合，下一步调参建议是什么？”*
6. **动态调优**：*“原先的 Plan 跑不通，根据最新实验现象帮我重新调整后续计划。”*

---

## 2. API 连接配置

- **Base URL**: `http://127.0.0.1:8000/v1`
- **API Key**: `sk-catgpt-c8d7daf612b7f95429424aaa3ab86f78fd771a8b0fc1c138`
- **Model**: `catgpt-browser`
- **协议**: OpenAI 标准兼容协议（支持 `/v1/chat/completions`、`/v1/responses` 等）

---

## 3. 极简接入与自由互动代码（Python）

本地 Agent 可以直接使用任何标准 OpenAI SDK 进行自由对话，也可以使用封装好的桥接模块：

### 方式 A：标准 OpenAI 客户端（最轻量、最通用）

```python
from openai import OpenAI

# 1. 创建客户端
client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="sk-catgpt-c8d7daf612b7f95429424aaa3ab86f78fd771a8b0fc1c138",
)

# 2. 维护上下文（保证多轮对话记忆）
messages = [
    {
        "role": "system",
        "content": "你是我的高阶 Plan Agent（架构师与实验智囊）。我负责在本地写代码和执行实验，遇到方案设计、代码审计、报错排查或实验分析时，我会随时向你请教。"
    }
]

def consult_plan_agent(user_prompt: str, stream: bool = True) -> str:
    """随时向 Plan Agent 发送任何问题，支持实时流式输出"""
    messages.append({"role": "user", "content": user_prompt})
    
    if stream:
        resp_stream = client.chat.completions.create(
            model="catgpt-browser",
            messages=messages,
            stream=True
        )
        chunks = []
        for chunk in resp_stream:
            if chunk.choices and chunk.choices[0].delta.content:
                delta = chunk.choices[0].delta.content
                print(delta, end="", flush=True)
                chunks.append(delta)
        reply = "".join(chunks)
    else:
        resp = client.chat.completions.create(
            model="catgpt-browser",
            messages=messages,
            stream=False
        )
        reply = resp.choices[0].message.content
        
    messages.append({"role": "assistant", "content": reply})
    return reply

# 随时按需使用示例：
# reply = consult_plan_agent("帮我规划一个目标检测实验...")
# reply = consult_plan_agent("帮我看看这段代码有没有 Bug: ...")
# reply = consult_plan_agent("实验跑完了，准确率 85%，下一步怎么做？")
```

---

### 方式 B：使用内置增强桥接器（`scripts.plan_agent_bridge`）

```python
from scripts.plan_agent_bridge import PlanAgentBridge

# 初始化智能体桥接
agent = PlanAgentBridge()

# 场景 1：自由发问（随时调用）
response = agent.ask("我们现在开始实现数据预处理模块，你有什么架构上的建议？")

# 场景 2：请求大纲规划
plan = agent.request_plan("在现有数据集上复现论文核心算法")

# 场景 3：提交代码审查
passed, review = agent.audit_code("Step 1", code_content="def train(): ...")

# 场景 4：突发异常求助
solution = agent.ask(f"运行发生崩溃，错误日志如下：\n{traceback_str}\n请给出修改方案。")

# 场景 5：提交实验指标
next_action = agent.submit_experiment_results("Step 1", "Loss: 0.23, mAP: 0.68")
```

---

## 4. 最佳实践提示

1. **共享上下文意识**：
   - 保持对话历史（`messages`），让 Plan Agent 随时知道之前做过什么、尝试过哪些方案，避免重复劳动。
2. **结构化提供输入**：
   - 提交代码或报错时，用 Markdown 代码块包裹（如 ````python ... ````），并附上简短的背景说明。
3. **即时求助，小步快跑**：
   - 本地 Agent 编写大模块前先问一句思路，写完核心函数先审一遍，遇到不确定时先确认，能极大减少盲目执行导致的资源浪费和重构成本。
