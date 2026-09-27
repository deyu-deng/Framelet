#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""开源归档门禁：把"哪个文件能出门"变成一条能跑的判据，而不是发布当天靠人眼挑。

为什么要有第 10 道门禁：档位这件事每期都会新增素材（剧照、封面、音色、3D 资产），
靠记忆挑文件必漏；而开源仓库的错误是**不可撤销**的 —— push 一次就永久公开。
所以清单是数据（Scripts/publish_manifest.json），判据是代码，两者不一致就 exit 1。

四条判据：
  1 覆盖    每个跟踪文件必须命中某一档；没归档 = 红
  2 不出门  never 档的文件不得被跟踪（声音样本、剧照、第三方来源素材）
  3 密钥    .env 里的任何一个值都不得出现在跟踪文件里（只报键名与文件，绝不打印值）
  4 历史    never 档路径若进过 git 历史，就是"别搬历史"的证据，报出来

用法：
    python Scripts/CheckPublish.py            # 全量检查
    python Scripts/CheckPublish.py --list     # 只打归档表，按档分组
退出码 1 = 有红项。
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "Scripts", "publish_manifest.json")
ORDER = ("never", "private", "public")       # 先命中者生效
SECRET_KEY_RE = re.compile(r"api_|key|secret|token|password|voice_id|group_id", re.I)

_bad, _note = [], []


def bad(section, msg):
    _bad.append(section)
    print("  [ FAIL ] %-12s %s" % (section, msg))


def ok(section, msg):
    print("  [ ok ]   %-12s %s" % (section, msg))


def note(section, msg):
    _note.append(section)
    print("  [ note ] %-12s %s" % (section, msg))


def regex(glob):
    """`**` 跨目录，`*` 只在一段内 —— 不用 fnmatch 是因为它的 * 会跨 /，
    那样 "Episodes/*" 会把整棵树都吃掉，档位就形同虚设。"""
    out = ""
    i = 0
    while i < len(glob):
        c = glob[i]
        if c == "*":
            if glob[i:i + 2] == "**":
                out += ".*"; i += 2; continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$")


def classify(path, tiers):
    """返回 (命中的档, 所有命中的档)。档序即优先级。"""
    hits = [t for t in ORDER if any(regex(g).match(path) for g in tiers[t])]
    return (hits[0] if hits else None), hits


def tracked(repo):
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo,
                         capture_output=True, text=True)
    return [p for p in out.stdout.split("\0") if p]


def ever_added(repo):
    out = subprocess.run(["git", "log", "--all", "--diff-filter=A",
                          "--name-only", "--pretty=format:"],
                         cwd=repo, capture_output=True, text=True)
    return {p for p in out.stdout.splitlines() if p.strip()}


def env_pairs():
    p = os.path.join(ROOT, ".env")
    if not os.path.isfile(p):
        return None
    pairs = []
    for line in open(p, encoding="utf-8", errors="replace"):
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip().strip('"').strip("'")
        # 只查名字像凭据的键。MINIMAX_TTS_MODEL 这种公开型号名进代码是应该的，
        # 拿它去全库搜只会制造假警报 —— 门禁一旦习惯喊狼，真泄漏就没人看。
        if not SECRET_KEY_RE.search(k):
            continue
        if len(v) < 8 or v.startswith(("/", "C:", "D:", "~")):
            continue
        if re.search(r"example|your|xxx|<.*>", v, re.I):
            continue
        pairs.append((k, v))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只打归档表")
    ap.add_argument("--repo", default=ROOT,
                    help="扫哪个仓库。默认本仓库；内容仓库用 --repo ~/Studio")
    args = ap.parse_args()

    # 扫别的仓库时，文件清单与档位规则跟着走，但 .env 仍然读工具自己那份 ——
    # 凭证只在工具仓库里，内容仓库本来就不该有 .env。
    repo = os.path.abspath(os.path.expanduser(args.repo))
    manifest = os.path.join(repo, "publish_manifest.json")
    if not os.path.isfile(manifest):
        manifest = MANIFEST
    tiers = json.load(open(manifest, encoding="utf-8"))
    files = tracked(repo)
    rows = [(f, classify(f, tiers)) for f in files]
    rules = sum(len(tiers[t]) for t in ORDER)

    if args.list:
        for t in ORDER:
            hit = [f for f, (a, _) in rows if a == t]
            print("== %s（%d）" % (t, len(hit)))
            for f in hit:
                print("   ", f)
        return 0

    if not files:
        # 扫到 0 个文件还报"全部通过"是最坏的一种绿：它会让人以为检查做过了。
        _fail = ("[FAIL] 索引里一个文件都没有 —— 先 git add，或者这里根本不是仓库。"
                 if not __import__("os").path.isdir(os.path.join(ROOT, ".git"))
                 else "[FAIL] 索引为空：没有东西可查，这不叫通过")
        print(_fail)
        return 1

    print("=== 开源归档检查（%d 个跟踪文件 / %d 条档位规则）==="
          % (len(files), rules))

    # 工作树已删、索引里还在：判档没意义（内容马上消失），单独报一行别混进红项
    missing = [f for f in files if not os.path.isfile(os.path.join(repo, f))]
    present = [f for f in files if f not in missing]
    if missing:
        note("待移除", "%d 个文件工作树里已删但索引还在：%s（提交后自动消失，不判档）"
             % (len(missing), "、".join(missing)))

    # 1 覆盖
    unclassified = [f for f, (a, _) in rows if a is None and f in present]
    if unclassified:
        bad("覆盖", "%d 个文件没归档：%s%s"
            % (len(unclassified), "\n      ".join(unclassified[:8]),
               "\n      …" if len(unclassified) > 8 else ""))
    else:
        ok("覆盖", "%d 个在位文件全部命中某一档" % len(present))

    # 2 不出门
    never = [f for f, (a, _) in rows if a == "never" and f in present]
    if never:
        bad("不出门", "%d 个 never 档文件正被 git 跟踪：%s"
            % (len(never), "、".join(never)))
        print("             这些是开源最贵的错误（声音样本可用来克隆本人声音）；"
              "要从索引里移除才算绿")
    else:
        ok("不出门", "没有 never 档文件被跟踪")

    # 遮蔽：一个文件同时命中多档 —— 优先级能定序，但规则写重了要让人看见。
    # 兜底档（"public": ["**"] 或 "private": ["**"]）不算写重，跳过，否则整仓都报重叠。
    def catch_all(hits):
        return len(hits) > 1 and any(set(tiers[t]) == {"**"} for t in hits)
    shadowed = [(f, h) for f, (_, h) in rows
                if len(h) > 1 and f in present and not catch_all(h)]
    if shadowed:
        note("档位重叠", "%d 个文件被多档命中，按 never>private>public 取前者，例：%s"
             % (len(shadowed), "、".join("%s(%s)" % (f, ">".join(h))
                                        for f, h in shadowed[:3])))

    # 3 密钥
    pairs = env_pairs()
    if pairs is None:
        note("密钥", "本机没有 .env，跳过值检索（不代表历史干净）")
    else:
        leaked = []
        blobs = {f: open(os.path.join(repo, f), "rb").read() for f in present}
        for k, v in pairs:
            needle = v.encode("utf-8")
            for f, data in blobs.items():
                if needle in data:
                    leaked.append("%s → %s" % (k, f))
        if leaked:
            bad("密钥", ".env 的值出现在跟踪文件里：%s" % "、".join(leaked))
        else:
            ok("密钥", "%d 个 .env 键值均未出现在 %d 个跟踪文件里"
               % (len(pairs), len(present)))

    # 4 历史
    hist = ever_added(repo)
    gone = sorted(p for p in hist if classify(p, tiers)[0] == "never")
    if gone:
        bad("历史", "%d 个 never 档路径进过 git 历史（现在删了也还在对象库里）：%s"
            % (len(gone), "、".join(os.path.basename(p) for p in gone)))
        print("             判据出处 Docs/OpenSourcePlan.md §3：开源走新建仓库 + 一次干净首提交，不搬历史")
    else:
        ok("历史", "never 档路径从未进过历史")

    counts = {t: sum(1 for _, (a, _) in rows if a == t) for t in ORDER}
    print("\n  归档统计  public %d ｜ private %d ｜ never %d"
          % (counts["public"], counts["private"], counts["never"]))

    if _bad:
        print("\n%d 项红：%s" % (len(set(_bad)), " / ".join(sorted(set(_bad)))))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
