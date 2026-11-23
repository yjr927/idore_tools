from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .openai_client import ModelCallError, default_model
from .pipeline import AnalysisConfig, analyze_run, prepare_script, render_existing
from .readers import ScriptReadError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="script-agent",
        description="从长篇剧本生成有证据的角色卡、成长弧线、关系档案和信息差台账。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="只在本地读取、切分剧本，不调用模型")
    prepare.add_argument("script", type=Path, help="TXT、Markdown、Fountain、PDF 或 DOCX 剧本")
    prepare.add_argument("--output", "-o", type=Path, required=True, help="本次运行目录")
    prepare.add_argument("--max-scene-chars", type=int, default=14000, help="超长单场的切分上限")

    analyze = subparsers.add_parser("analyze", help="准备剧本并执行完整分析")
    analyze.add_argument("script", type=Path, help="TXT、Markdown、Fountain、PDF 或 DOCX 剧本")
    _add_analysis_arguments(analyze)
    analyze.add_argument("--max-scene-chars", type=int, default=14000, help="超长单场的切分上限")

    resume = subparsers.add_parser("resume", help="从已有运行目录继续，自动复用已完成场景")
    resume.add_argument("run_dir", type=Path, help="之前 analyze/prepare 创建的目录")
    _add_analysis_arguments(resume, include_output=False)

    render = subparsers.add_parser("render", help="根据已有 report.json 重新生成网页和 Markdown")
    render.add_argument("run_dir", type=Path)

    return parser


def _add_analysis_arguments(parser: argparse.ArgumentParser, include_output: bool = True) -> None:
    if include_output:
        parser.add_argument("--output", "-o", type=Path, required=True, help="本次运行目录")
    parser.add_argument("--extract-model", default=default_model(), help="逐场提取模型")
    parser.add_argument("--synthesis-model", default=default_model(), help="角色与关系汇总模型")
    parser.add_argument("--window-scenes", type=int, default=12, help="每个阶段最多包含的相关场景数")
    parser.add_argument("--max-output-tokens", type=int, default=12000)
    parser.add_argument("--workers", type=int, default=2, help="可并行的模型请求数")
    parser.add_argument("--fast", action="store_true", help="跳过中间阶段汇总，快速生成预览")
    parser.add_argument("--force", action="store_true", help="忽略缓存，重新调用模型")


def _config(args: argparse.Namespace) -> AnalysisConfig:
    return AnalysisConfig(
        extract_model=args.extract_model,
        synthesis_model=args.synthesis_model,
        window_scenes=max(1, args.window_scenes),
        max_output_tokens=max(1000, args.max_output_tokens),
        max_workers=max(1, args.workers),
        fast_mode=args.fast,
        force=args.force,
    )


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            manifest = prepare_script(args.script, args.output, args.max_scene_chars)
            print("已在本地切分 {} 个场景：{}".format(manifest["scene_count"], args.output.resolve()))
            return
        if args.command == "analyze":
            manifest = prepare_script(args.script, args.output, args.max_scene_chars)
            print("已准备 {} 个场景，开始分析。".format(manifest["scene_count"]))
            analyze_run(args.output, _config(args), progress=print)
            return
        if args.command == "resume":
            analyze_run(args.run_dir, _config(args), progress=print)
            return
        if args.command == "render":
            render_existing(args.run_dir)
            print("报告已重新生成：{}".format((args.run_dir / "output" / "report.html").resolve()))
            return
    except (RuntimeError, ScriptReadError, ModelCallError) as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        raise SystemExit(1)
