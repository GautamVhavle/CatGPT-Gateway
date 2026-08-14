"""
Plan Agent Bridge
-----------------
用于本地 Execution Agent（执行智能体）与 Plan Agent（本 API 驱动的规划/审计/决策智囊）
进行随时随地、自由按需多轮协同的轻量 SDK 封装。
"""

from __future__ import annotations

import re
from openai import OpenAI


class PlanAgentBridge:
    """双 Agent 随需协同桥接器"""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000/v1",
        api_key: str = "sk-catgpt-c8d7daf612b7f95429424aaa3ab86f78fd771a8b0fc1c138",
        model: str = "catgpt-browser",
    ):
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "你是一个先进的 Plan Agent（高阶架构师、实验智囊与代码审计专家）。"
                    "本地执行智能体（Execution Agent）在遇到方案设计、代码编写、Bug 排查、"
                    "实验分析或方向决策时，会随时向你咨询。"
                    "请根据其提供的实时上下文，给出清晰、严谨、具备高度指导性的建议与指令。"
                ),
            }
        ]

    def ask(self, prompt: str, stream: bool = True) -> str:
        """
        核心自由问答接口：本地 Agent 可以在任何时刻发送问题，支持实时流式输出打印。
        """
        self.messages.append({"role": "user", "content": prompt})

        if stream:
            response_stream = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                stream=True,
            )
            collected_chunks: list[str] = []
            print("\n[Plan Agent 实时生成中]:\n", flush=True)
            for chunk in response_stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    delta = chunk.choices[0].delta.content
                    print(delta, end="", flush=True)
                    collected_chunks.append(delta)
            print("\n" + "-" * 50 + "\n", flush=True)
            full_content = "".join(collected_chunks)
            self.messages.append({"role": "assistant", "content": full_content})
            return full_content
        else:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                stream=False,
            )
            content = response.choices[0].message.content or ""
            self.messages.append({"role": "assistant", "content": content})
            return content

    def request_plan(self, objective: str, env_context: str = "") -> str:
        """快捷调用：请求架构规划与阶段性实施方案"""
        prompt = (
            f"[PLANNING REQUEST]\n"
            f"【目标】: {objective}\n"
            f"【环境/上下文】: {env_context or '标准环境'}\n\n"
            f"请提供清晰的架构设计、实施步骤与实验计划。"
        )
        return self.ask(prompt)

    def audit_code(self, context_desc: str, code_content: str, language: str = "python") -> tuple[bool, str]:
        """快捷调用：提交代码/模块供审计"""
        prompt = (
            f"[CODE AUDIT REQUEST]\n"
            f"【模块描述】: {context_desc}\n"
            f"【代码】:\n```{language}\n{code_content}\n```\n\n"
            f"请审查代码正确性与安全性。若完全合格请在末尾附上 `[AUDIT_PASS]`，若需修改请附上 `[AUDIT_REVISE]`。"
        )
        feedback = self.ask(prompt)
        passed = "[AUDIT_PASS]" in feedback or "[AUDIT: PASS]" in feedback
        return passed, feedback

    def submit_experiment_results(self, step_desc: str, result_logs: str) -> str:
        """快捷调用：汇报实验结果/日志，请求下一步指令"""
        prompt = (
            f"[EXPERIMENT RESULT]\n"
            f"【实验阶段】: {step_desc}\n"
            f"【输出与指标】:\n{result_logs}\n\n"
            f"请分析上述实验结果并下发下一步具体行动建议。"
        )
        return self.ask(prompt)

    def clear_history(self):
        """清空会话历史（开启全新任务时使用）"""
        self.messages = [self.messages[0]]


if __name__ == "__main__":
    bridge = PlanAgentBridge()
    print("PlanAgentBridge ready for on-demand consultations.")
