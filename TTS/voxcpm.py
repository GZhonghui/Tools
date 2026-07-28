#!/usr/bin/env python3
"""VoxCPM2 长文本 TTS：克隆参考音频的音色，把一个 txt 逐段合成为一整段音频。

配置都在下面的常量里，改完直接运行：
    /export/home/zh-ge/pyenvs/tts312/bin/python main.py

参考：https://wiki.gzher.com/doku.php?id=%E8%BD%AF%E4%BB%B6:tts:voxcpm2
"""

import re
from pathlib import Path

import numpy as np
import soundfile as sf
from voxcpm import VoxCPM

# --- 配置 ---------------------------------------------------------------

MODEL_DIR = "/home/zh-ge/models/VoxCPM2"  # 本地模型目录
TEXT_FILE = "input.txt"     # 待朗读的长文本，UTF-8
OUTPUT_WAV = "output.wav"   # 输出音频，48kHz
REF_AUDIO = "ref.wav"       # 音色参考音频，3~10 秒干净人声最佳
# 参考音频的原文。二选一：留空则文中的 (指令) 生效；填上音色最保真，但模型收到的
# 是 REF_TEXT + 正文，(指令) 不在开头就会被当正文念出来。
# 例："本音声由步非烟工作室出品。QQ号：2485694364。请大家支持正版。"
REF_TEXT = "本音声由步非烟工作室出品。QQ号：2485694364。请大家支持正版。"

TARGET_CHARS = 120   # 每段目标长度，汉字计，约 30 秒音频
SENT_GAP = 0.25      # 段内片段之间的静音秒数
PARA_GAP = 0.5       # 段落之间的静音秒数

CFG_VALUE = 2.0           # 引导强度，1.0~3.0，越大越贴近参考音色
INFERENCE_TIMESTEPS = 10  # 扩散步数，4~30，越大质量略好但更慢
NORMALIZE = True          # 文本归一化，让数字、符号按中文读法念

# --- 文本切分 -----------------------------------------------------------

# 句末：中文标点之后（收尾引号之前不切），或西文标点后紧跟空白处。
# 西文规则顺带避开了小数点，"3.14" 的点后面不是空白。
_SENT = re.compile(r"(?<=[。！？；…])(?![”’」』\)])|(?<=[.!?;])(?=\s)")
_SUB = re.compile(r"(?<=[，,、；;：:])")          # 次级切分点，只在单句过长时用
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")
# 情感/风格指令。模型只认片段开头的括号，所以文中的括号要提到开头去
_STYLE = re.compile(r"\(([^)]{1,30})\)")


def wlen(text):
    """估算朗读长度：CJK 字符算 1，其余算 0.4，中英混排的分段时长才可比。"""
    cjk = len(_CJK.findall(text))
    return cjk + 0.4 * (len(text) - cjk)


def split_styles(para):
    """按 (指令) 把段落切成 [(指令, 文本)]，一个指令作用到下一个指令为止。"""
    parts, pos, style = [], 0, ""
    for m in _STYLE.finditer(para):
        if para[pos : m.start()].strip():
            parts.append((style, para[pos : m.start()]))
        style, pos = m.group(1).strip(), m.end()
    if para[pos:].strip():
        parts.append((style, para[pos:]))
    return parts


def pack_sentences(text):
    """切句后累积成接近 TARGET_CHARS 的片段。"""
    # 超过目标长度的句子按次级标点切碎，下面重新拼回接近目标的长度。
    # 这里不 strip，好留住西文单词间的空格
    units = []
    for sent in _SENT.split(text):
        if sent.strip():
            units.extend(_SUB.split(sent) if wlen(sent) > TARGET_CHARS else [sent])

    out, buf = [], []
    for unit in units:
        if buf and wlen("".join(buf) + unit) > TARGET_CHARS:
            out.append("".join(buf).strip())
            buf = []
        buf.append(unit)
    if buf:
        out.append("".join(buf).strip())
    return out


def split_text(raw):
    """把长文本切成 [(片段文本, 该片段之后的静音秒数)]。

    空行分隔的段落强制断开，段内按句子累积到 TARGET_CHARS。文中的（指令）
    会转成半角、提到片段开头，并跟着后续每个片段走，直到下一个指令出现。
    """
    paragraphs = [p for p in re.split(r"\n\s*\n+", raw) if p.strip()]
    chunks = []

    for pi, para in enumerate(paragraphs):
        para = re.sub(r"\s+", " ", para).strip()
        para = para.replace("（", "(").replace("）", ")")

        # 摘掉所有合法指令后还剩括号，说明括号不成对或指令过长，宁可报错也别念出来
        if set("()") & set(_STYLE.sub("", para)):
            raise SystemExit(
                f"第 {pi + 1} 段的括号不成对，或指令超过 30 字：\n{para[:80]}"
            )

        texts = []
        for style, seg in split_styles(para):
            texts.extend(f"({style}){t}" if style else t for t in pack_sentences(seg))

        for ci, text in enumerate(texts):
            end_of_para = ci == len(texts) - 1
            end_of_all = end_of_para and pi == len(paragraphs) - 1
            chunks.append(
                (text, 0.0 if end_of_all else PARA_GAP if end_of_para else SENT_GAP)
            )

    return chunks


# --- 合成 ---------------------------------------------------------------


def main():
    chunks = split_text(Path(TEXT_FILE).read_text(encoding="utf-8"))
    if not chunks:
        raise SystemExit(f"{TEXT_FILE} 里没有可朗读的文本")
    print(f"共 {len(chunks)} 段待合成")
    styles = sorted({m.group(1) for t, _ in chunks if (m := _STYLE.match(t))})
    if styles:
        # 正文里普通用途的括号也会被当成指令，指令本身不会被念出来，留意这里的输出
        print(f"识别到括号指令：{'、'.join(styles)}")

    model = VoxCPM(voxcpm_model_path=MODEL_DIR, enable_denoiser=False)
    sr = model.tts_model.sample_rate

    # 只传 reference 时正文原样进模型，开头的 (指令) 才会被当成风格控制；
    # 补上 prompt 就是官方的最高保真档，但模型收到的文本变成 REF_TEXT + 正文
    clone = {"reference_wav_path": REF_AUDIO}
    if REF_TEXT:
        clone["prompt_wav_path"] = REF_AUDIO
        clone["prompt_text"] = REF_TEXT

    parts = []
    for i, (text, gap) in enumerate(chunks, 1):
        wav = model.generate(
            text=text,
            **clone,
            cfg_value=CFG_VALUE,
            inference_timesteps=INFERENCE_TIMESTEPS,
            normalize=NORMALIZE,
        )
        print(f"[{i}/{len(chunks)}] {len(wav) / sr:5.1f}s")
        parts.append(wav)
        if gap:
            parts.append(np.zeros(int(sr * gap), dtype=np.float32))

    audio = np.concatenate(parts)
    sf.write(OUTPUT_WAV, audio, sr, subtype="PCM_16")
    print(f"完成：{OUTPUT_WAV}（{len(audio) / sr / 60:.1f} 分钟）")


if __name__ == "__main__":
    main()
