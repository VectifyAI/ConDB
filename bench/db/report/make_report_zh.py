#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a Chinese-language PDF of the storage-engine benchmark report with
reportlab (the LaTeX CJK toolchain is unavailable in this environment). Mirrors
bench/db/report/report.tex. Run: python3 make_report_zh.py"""
import os
from PIL import Image as PILImage
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm, mm
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, Image, KeepTogether)
from reportlab.lib.styles import ParagraphStyle

NOTO = "/usr/share/fonts/opentype/noto"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "report_zh.pdf")
FONTDIR = "/tmp"  # converted TTFs cached here

def _build_ttf(src, out):
    # reportlab needs TrueType outlines; Noto CJK ships CFF, so subset the SC
    # face to the chars this script uses and convert CFF->glyf with cu2qu.
    from fontTools.ttLib import TTFont as FTFont, newTable
    from fontTools import subset
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    text = "".join(sorted(set(open(__file__, encoding="utf-8").read()) |
                          set("0123456789.,/-–—→≤×≈%() ")))
    f = FTFont(src, fontNumber=2)  # SC face inside the .ttc
    ss = subset.Subsetter(); ss.populate(text=text); ss.subset(f)
    order, gs, quad = f.getGlyphOrder(), f.getGlyphSet(), {}
    for n in order:
        pen = TTGlyphPen(gs); gs[n].draw(Cu2QuPen(pen, 1.0)); quad[n] = pen.glyph()
    glyf = newTable("glyf"); glyf.glyphOrder = order; glyf.glyphs = quad; f["glyf"] = glyf
    mp = f["maxp"]; mp.tableVersion = 0x00010000; mp.maxZones = 1
    for a in ("maxTwilightPoints", "maxStorage", "maxFunctionDefs", "maxInstructionDefs",
              "maxStackElements", "maxSizeOfInstructions", "maxComponentElements", "maxComponentDepth"):
        setattr(mp, a, 0)
    f["head"].indexToLocFormat = 0
    if "post" in f: f["post"].formatType = 3.0
    for t in ("CFF ", "CFF2", "VORG"):
        if t in f: del f[t]
    if "loca" not in f: f["loca"] = newTable("loca")
    f.sfntVersion = "\000\001\000\000"
    f.save(out)

def _font(name, ttf, ttc):
    path = os.path.join(FONTDIR, ttf)
    if not os.path.exists(path):
        _build_ttf(os.path.join(NOTO, ttc), path)
    pdfmetrics.registerFont(TTFont(name, path))

_font("SC", "SerifSC.ttf", "NotoSerifCJK-Regular.ttc")
_font("SCb", "SerifSCb.ttf", "NotoSerifCJK-Bold.ttc")
pdfmetrics.registerFontFamily("SC", normal="SC", bold="SCb", italic="SC", boldItalic="SCb")

def C(s):  # inline monospace (code spans are ASCII -> built-in Courier)
    return f'<font name="Courier">{s}</font>'

st_title = ParagraphStyle("title", fontName="SCb", fontSize=16, leading=22, alignment=TA_CENTER, spaceAfter=4)
st_auth = ParagraphStyle("auth", fontName="SC", fontSize=11, leading=15, alignment=TA_CENTER, spaceAfter=14)
st_h1 = ParagraphStyle("h1", fontName="SCb", fontSize=13, leading=18, spaceBefore=12, spaceAfter=5)
st_h2 = ParagraphStyle("h2", fontName="SCb", fontSize=11, leading=15, spaceBefore=8, spaceAfter=3)
st_body = ParagraphStyle("body", fontName="SC", fontSize=10, leading=16, alignment=TA_JUSTIFY, spaceAfter=6)
st_cap = ParagraphStyle("cap", fontName="SC", fontSize=8.5, leading=12, alignment=TA_CENTER,
                        textColor=colors.HexColor("#333333"), spaceBefore=3, spaceAfter=8)
st_abs = ParagraphStyle("abs", fontName="SC", fontSize=9.5, leading=15, alignment=TA_JUSTIFY,
                        leftIndent=6, rightIndent=6, spaceAfter=10)

def P(t, s=st_body): return Paragraph(t, s)
def H1(t): return Paragraph(t, st_h1)
def H2(t): return Paragraph(t, st_h2)
def CAP(t): return Paragraph(t, st_cap)

HDR = colors.HexColor("#ebebeb")
KEY = colors.HexColor("#e2eff6")

def table(data, aligns, widths, header_rows=1, key_row=None, bold=None, fs=8.5):
    bold = bold or []
    cells = [[Paragraph(str(c), ParagraphStyle("c", fontName="SCb" if (r < header_rows or (r, ci) in bold) else "SC",
                                               fontSize=fs, leading=fs + 3,
                                               alignment={"l": TA_LEFT, "r": 2, "c": TA_CENTER}[aligns[ci]]))
              for ci, c in enumerate(row)] for r, row in enumerate(data)]
    t = Table(cells, colWidths=widths, hAlign="CENTER")
    style = [("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
             ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
             ("LINEABOVE", (0, 0), (-1, 0), 1.0, colors.black),
             ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.5, colors.black),
             ("LINEBELOW", (0, -1), (-1, -1), 1.0, colors.black),
             ("BACKGROUND", (0, 0), (-1, header_rows - 1), HDR),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    if key_row is not None:
        style.append(("BACKGROUND", (0, key_row), (-1, key_row), KEY))
    t.setStyle(TableStyle(style))
    return t

def _fig(name):  # convert figures/<name>.pdf -> /tmp png on demand
    import subprocess
    out = f"/tmp/zhfig_{name}"
    if not os.path.exists(out + "-1.png"):
        subprocess.run(["pdftoppm", "-png", "-r", "200",
                        os.path.join(HERE, "figures", f"{name}.pdf"), out], check=True)
    return out + "-1.png"

def figure(name, frac=0.62):
    png = _fig(name)
    im = PILImage.open(png); w, h = im.size
    tw = (A4[0] - 4 * cm) * frac
    return Image(png, width=tw, height=tw * h / w, hAlign="CENTER")

F = []
F += [P("用于 PageIndex 检索的存储引擎:<br/>MongoDB、PostgreSQL、DuckDB 与 SQLite 的工作负载基准", st_title),
      P("Junyao Dong", st_auth)]

# Abstract
F += [P("<b>摘要。</b> PageIndex 把文档变成大型分层 JSON 树,智能体从根节点向下导航到相关节点。"
        "本报告测量与 ConDB 检索相对应的存储级操作,跨引擎结论只采用对称配置。单独的 300 万节点公平矩阵中,"
        "MongoDB 与 PostgreSQL 使用相同 200 条子树路径、返回相同节点 id、并获得等价的覆盖索引。"
        "在相同单体布局的 covered arm 上,MongoDB 的 P50/P95 分别慢 4.35/3.91 倍;双方都使用窄结构表/集合时,"
        "分别慢 4.65/4.07 倍。另一个 MongoDB 单引擎的一千万节点消融显示,覆盖索引能做到零文档取出并达到 "
        "P50 15.5 ms / P95 168 ms,但该结果不用于跨引擎相除。结构、元数据、正文三路解耦仍是候选 schema;"
        "其缓存常驻收益和完整端到端代价尚待部署形状的验证。", st_abs)]

# 1 Introduction
F += [H1("1　引言"),
      P(f"ConDB 把一篇文档索引成一棵 PageIndex 树:节点构成的层级结构,每个节点带有标题、摘要、"
        "页码索引,叶子节点还带有正文。检索是一次自顶向下的遍历:检索器从根节点出发,列出某个节点的"
        "子节点以决定向哪里下探,拉取一棵子树供语言模型阅读,再获取它所选中节点的内容。本项目要回答的"
        "问题是:文档型存储(MongoDB)是否适合存放这类数据;如果适合,该优化什么。本报告用实测数据和一份"
        "具体方案回答这两点。"),
      P("我们刻意只对 ConDB 实际发出的操作做基准。检索路径从不执行的通用数据库操作(从节点回溯到根、"
        "页码区间过滤、朴素子串扫描)被排除在外,因为它们的结果无助于优化。")]

# 2 Workload and methodology
F += [H1("2　工作负载与方法"),
      H2("操作"),
      P(f"表 1 把所测存储原语映射到 ConDB 检索操作。{C('get_subtree')} 微基准只测返回节点 id 的物化路径"
        "区间扫描,不包括根节点读取、元数据解析、树格式化或分页;这些步骤被明确列为端到端验证缺口。")]
F += [table([["操作", "存储调用", "用途"],
             ["点查", C("get_node(tree_id, node_id)"), "导航到 / 读取单个节点"],
             ["展开子节点", C("get_children(tree_id, node_id)"), "列出子节点,决定下探方向"],
             ["取子树", C("get_subtree(tree_id, node_id, depth)"), "渲染树视图、拉取一个 block"],
             ["取内容", C("get_entity(tree_id, node_id)"), "读取所选节点的内容"]],
            ["l", "l", "l"], [2.2 * cm, 6.4 * cm, 6.2 * cm], key_row=3),
      CAP("表 1:ConDB 检索发出的存储操作。")]
F += [H2("数据"),
      P(f"采用规范格式的合成 PageIndex 树,并用 ConDB 的 {C('DocumentTreeAdapter')} 校验,两种规模(表 2)。"
        "内容是随机词;基准考察的是树的形状与每节点的负载大小。")]
F += [table([["数据集", "节点数", "JSON 大小", "深度", "扇出"],
             ["中(medium)", "70,843", "85 MB", "5", "6–12"],
             ["大(large)", "10,000,000", "14.06 GB", "8", "6–14"]],
            ["l", "r", "r", "r", "r"], [3.0 * cm, 3.0 * cm, 3.0 * cm, 2.0 * cm, 2.5 * cm]),
      CAP("表 2:数据集。")]
F += [H2("引擎与环境"),
      P("MongoDB 7.0(社区版)与 PostgreSQL 16(JSONB 文档列)以服务端形式跑在 Docker 中、经 localhost 访问;"
        "DuckDB 与 SQLite 为嵌入式。原始中/大型套件在 96 核、1 TB 内存主机上使用相同单体布局。"
        "跨引擎子树结论只来自另一轮干净的 16 vCPU/30 GiB 主机实验:300 万节点、每次只跑一个引擎、"
        "相同 200 条路径(平均返回 8,840.4 个 id)。MongoDB 与 PostgreSQL 都依次测试单体+普通 path 索引、"
        "单体+覆盖索引、窄结构 relation+覆盖索引。PG 使用 (path) INCLUDE(node_id) 并 VACUUM ANALYZE;"
        "MongoDB 使用 {path,node_id} 且投影排除 _id;两边的 explain 都确认 index-only。"
        "一千万节点的 15.5/168 ms 只属于另一轮 MongoDB 单引擎消融。"),
      H2("公平性说明"),
      P("SQLite 与 DuckDB 在进程内运行,一次查询就是一次函数调用;MongoDB 与 PostgreSQL 要付每次调用的"
        "客户端–服务端开销(一次 localhost 往返加协议处理),嵌入式引擎从不承担。因此公平的单次调用比较是 "
        "MongoDB 对 PostgreSQL;跨越嵌入式/服务端这条线的绝对微秒数不可比,而并发一节(各自独立进程、无 GIL)"
        f"展示的是真实的服务端吞吐。MongoDB 的 {C('storageSize')} 是压缩后的(WiredTiger snappy),其它是未压缩的"
        "磁盘文件,所以 MongoDB 的未压缩逻辑大小单独列出;且随机词内容大概比真实散文更易压缩,使压缩后的数字偏好看。"
        "每项横比都只在同一 campaign 的同一主机内进行;到并发为止的各项用一库一树,多租户、批量读取与删除"
        "在下文用 25 棵共存树单独测量。")]

# 3 Results
F += [H1("3　结果"), H2("3.1　存储与摄入")]
F += [table([["引擎", "磁盘合计", "索引", "未压缩", "摄入(节点/秒)", "建表"],
             ["DuckDB", "5.02 GB", "0", "—", "348,831", "10 s"],
             ["MongoDB", "5.81 GB", "536 MB", "15.21 GB", "41,387", "69 s"],
             ["PostgreSQL (JSONB)", "17.71 GB", "1.19 GB", "—", "62,816", "52 s"],
             ["SQLite", "19.51 GB", "980 MB", "—", "52,880", "38 s"]],
            ["l", "r", "r", "r", "r", "r"],
            [4.0 * cm, 2.2 * cm, 1.8 * cm, 2.2 * cm, 3.2 * cm, 1.6 * cm],
            bold=[(1, 1), (1, 4), (1, 5)]),
      CAP("表 3:存储与摄入(大数据集)。磁盘合计含索引。MongoDB 以 5.27 GB 磁盘数据(WiredTiger snappy)"
          "承载 15.21 GB 逻辑 BSON。")]
F += [P("DuckDB 与 MongoDB 最紧凑(表 3),DuckDB 靠列式编码、MongoDB 靠压缩;即便未压缩,MongoDB 的 "
        "15.21 GB BSON 也不比关系型引擎的原始表更大。PostgreSQL 与 SQLite 在磁盘上要大上数倍。")]
F += [H2("3.2　检索操作")]
F += [P(f"导航读取(点查、子节点、取内容)在除 DuckDB 外的每个引擎上都在亚毫秒级,DuckDB 并非为 OLTP 点读"
        f"而生。在这些读取上 MongoDB 每次调用约比 PostgreSQL 慢 3 倍(表 4),其中一部分是每次调用的客户端开销,"
        "但在处处远低于一毫秒的量级上,这点差距没有实际代价。触达大量记录的子树扫描不再与这些单记录读取"
        "混表,而只在表 4b 的对称矩阵中比较。covered-to-covered 下,MongoDB 的 P50/P95 分别比 PostgreSQL "
        "慢 4.35/3.91 倍;双方都用窄结构 relation 时为 4.65/4.07 倍。第 4 节只解释 MongoDB 内部可消除的 FETCH。")]
F += [table([["操作", "MongoDB", "PostgreSQL", "DuckDB", "SQLite"],
             ["中数据集,P50", "", "", "", ""],
             ["点查 (get_node)", "0.238", "0.081", "2.705", "0.008"],
             ["展开子节点 (get_children)", "0.251", "0.090", "0.856", "0.017"],
             ["取内容 (get_entity)", "0.228", "0.071", "2.750", "0.007"],
             ["大数据集,P50 / P95", "", "", "", ""],
             ["点查", "0.270 / 0.32", "0.087 / 0.09", "5.59 / 8.97", "0.012 / 0.014"],
             ["展开子节点", "0.282 / 0.32", "0.099 / 0.14", "4.28 / 5.05", "0.023 / 0.032"],
             ["取内容", "0.249 / 0.32", "0.081 / 0.09", "5.21 / 8.35", "0.012 / 0.013"]],
            ["l", "r", "r", "r", "r"],
            [5.2 * cm, 2.7 * cm, 2.7 * cm, 2.5 * cm, 2.6 * cm], header_rows=1,
            bold=[(2, 4), (3, 4), (4, 4), (6, 4), (7, 4), (8, 4)]),
      CAP("表 4a:相同单体布局上的单记录检索延迟(ms,越低越好)。子树扫描不在此表横比。"),
      table([["对称 arm (P50 / P95 ms)", "MongoDB", "PostgreSQL", "Mongo / PG"],
             ["单体 + 普通 path 索引", "2.391 / 19.201", "0.648 / 6.817", "3.69 / 2.82×"],
             ["单体 + covering index", "1.291 / 10.692", "0.297 / 2.732", "4.35 / 3.91×"],
             ["窄 structure + covering", "1.269 / 10.616", "0.273 / 2.610", "4.65 / 4.07×"]],
            ["l", "r", "r", "r"], [6.1 * cm, 3.1 * cm, 3.1 * cm, 3.0 * cm],
            key_row=2, bold=[(1, 2), (2, 2), (3, 2)]),
      CAP("表 4b:公平的 ids-only get_subtree 矩阵。300 万节点、相同 200 条路径、平均返回 8,840.4 个 id;"
          "每一行都只比较双方相同 arm。"),
      figure("subtree"),
      CAP("图 1:表 4b 的 MongoDB–PostgreSQL 对称三臂结果(对数刻度),不存在优化 Mongo 对未优化 PG。"),
      P("SQLite 也跑了对称三臂,但它是进程内引擎,不用于服务端主比例。DuckDB 更特殊:列存投影天然只读 node_id 列,"
        "covered 与 naive 按构造等价,且没有独立 narrow-structure arm,因此不参加三臂排名。")]
F += [H2("3.3　写入路径"),
      P("当前设置下,PostgreSQL 的更新延迟低于 MongoDB(表 5),但该实验没有统一 durability 设置,"
        "因此只作描述,不用于公平吞吐排名。5,000 次更新也只能支持“本短跑未测到增长”,不能证明长期永不膨胀。")]
F += [table([["引擎", "更新 P50", "更新 ops/s", "插入 ops/s", "膨胀"],
             ["MongoDB", "0.242", "4,050", "5,586", "0 MB"],
             ["PostgreSQL (JSONB)", "0.123", "7,856", "8,732", "7 MB"],
             ["DuckDB", "2.966", "332", "450", "7 MB"],
             ["SQLite", "0.017", "40,442", "21,815", "0 MB"]],
            ["l", "r", "r", "r", "r"],
            [4.2 * cm, 2.4 * cm, 2.6 * cm, 2.6 * cm, 2.0 * cm], key_row=1),
      CAP("表 5:写入路径(中数据集):5,000 次字段更新,2,000 次增量插入。durability 未统一,不据此评胜负。")]
F += [H2("3.4　并发"),
      P("记录到的单次五秒点查中(图 2),MongoDB 从 1 到 2 客户端先下降、随后一直升到 64;PostgreSQL 的最高值"
        "也在 64;SQLite 在 8 个客户端见顶后下降。worker 没有 start barrier,每档也只跑一次,所以本图只作"
        "描述性扩展证据,不能当作精确排名或稳定饱和点。"),
      figure("concurrency"),
      CAP("图 2:点查吞吐随并发客户端进程数的变化(中数据集)。")]

# 3.5 added section
F += [H2("3.5　多租户、批量读取与删除"),
      P(f"把检索器的调用映射到存储,会浮现出前几节遗漏的三个操作:多树共存于一个存储时每次查询的租户过滤、"
        f"导航步发出的批量 id 列表读取,以及整棵树的删除。我们在把中数据集树复制成 25 棵共存树(单一存储内 "
        f"1,771,075 行)上测量它们,每次读取都按 {C('tree_id')} 过滤,由以 {C('tree_id')} 打头的复合索引服务。"),
      P(f"<b>多租户选择性。</b> 25 棵树同处一个集合时,以 {C('tree_id')} 打头的索引让每次读取都贴近其单树延迟"
        "(表 6):代价随索引深度变化,而非随共存树的数量。对一个大 25 倍的存储,MongoDB 在点查与子节点"
        "读取上约升 1.5 倍(对照表 4),没有扫描爆炸,且 MongoDB 与 PostgreSQL 的排序不变。")]
F += [table([["操作", "MongoDB", "PostgreSQL", "DuckDB", "SQLite"],
             ["点查", "0.345", "0.124", "3.772", "0.008"],
             ["展开子节点", "0.372", "0.147", "3.073", "0.014"]],
            ["l", "r", "r", "r", "r"],
            [4.2 * cm, 2.6 * cm, 2.6 * cm, 2.4 * cm, 2.4 * cm],
            bold=[(1, 4), (2, 4)]),
      CAP("表 6:25 棵树共存于单一存储、各按 tree_id 过滤的每次查询读取(P50,ms)。")]
F += [P(f"<b>批量读取。</b> 导航步返回一组候选节点 id,因此检索器一次取多个节点。一次批量读取"
        f"(MongoDB 用 {C('$in')}、关系型引擎用 {C('IN')})在每个引擎上都胜过它所替代的点查循环,"
        "且差距随批量增大而扩大,因为在服务端引擎上,循环的每一步都是一次往返。在 200 个 id 时,MongoDB "
        "批量 4.6 ms,而循环 273 ms,差 60 倍;PostgreSQL 是 1.4 ms 对 29 ms(表 7)。MongoDB 受益最大,"
        "因为批量摊薄了它在单次读取上所付的每次调用客户端开销;进程内的 SQLite 没有往返可摊,只快约 2 倍。"
        "检索器的做法应是用一次批量读取解析候选集,而不是循环。")]
F += [table([["引擎", "10", "50", "200", "循环 / 200"],
             ["MongoDB", "0.46", "1.56", "4.56", "273"],
             ["PostgreSQL (JSONB)", "0.27", "0.78", "1.40", "29"],
             ["DuckDB", "19.6", "22.2", "39.1", "790"],
             ["SQLite", "0.03", "0.13", "0.50", "0.97"]],
            ["l", "r", "r", "r", "r"],
            [4.4 * cm, 2.2 * cm, 2.2 * cm, 2.2 * cm, 2.6 * cm],
            bold=[(4, 1), (4, 2), (4, 3), (4, 4)]),
      CAP("表 7:批量 id 列表读取在 10/50/200 个 id 时,对照它所替代的 200-id 点查循环(P50,ms/批)。"
          "前三列为批量读取。")]
F += [P(f"<b>删除。</b> 删掉一棵树(此处为一个租户、1,771,075 行中的 70,843 行)是生命周期操作,不属于检索。"
        "删除本身在每个引擎上都快(不到四秒),但仅靠删除没有一个引擎会归还磁盘空间(表 8)。SQLite"
        f"({C('VACUUM')})与 PostgreSQL({C('VACUUM FULL')})会通过一次全量、加锁的重写收回被删树的占用,"
        f"耗时 10 至 17 秒;MongoDB 的 {C('compact')} 与 DuckDB 对单租户删除并未让文件明显缩小,这是 "
        "WiredTiger 删除后不向操作系统归还空间的既定行为。对 MongoDB 而言这与写入路径对称:WiredTiger 在更新下"
        f"不背负 vacuum 债,代价则是一次性批量删除不会自动缩容。WiredTiger 以检查点块为粒度报告 {C('storageSize')},"
        "所以几十 MB 以下的变化是报告粒度,而非已回收的空间。")]
F += [table([["引擎", "删除 行/秒", "回收", "回收耗时", "磁盘(前 → 后)"],
             ["PostgreSQL (JSONB)", "157,578", C("VACUUM FULL"), "17.3 s", "2.77 → 2.66 GB"],
             ["SQLite", "141,842", C("VACUUM"), "10.6 s", "2.56 → 2.46 GB"],
             ["MongoDB", "60,680", C("compact"), "0.1 s", "911 MB,无缩容"],
             ["DuckDB", "20,171", "checkpoint", "—", "790 MB,无缩容"]],
            ["l", "r", "l", "r", "l"],
            [4.0 * cm, 2.3 * cm, 2.7 * cm, 2.0 * cm, 3.6 * cm]),
      CAP("表 8:整树删除与空间回收(在 25 棵树中删除其一)。")]
F += [P("这三项都不改变本报告的结论:MongoDB 在大规模下守住租户过滤、在批量读取上完胜,删除上只在一个"
        "检索从不触发的、不加锁的缩容上落后。")]

# 4 Diagnosis
F += [H1(f"4　{C('get_subtree')} 诊断:取出的次数,而非大小"),
      P("压力是结构性的,而非配置失误。MongoDB 的读取单位是整份文档。一个二级索引(这里在 "
        f"{C('path')} 上)是一棵只存被索引字段与记录 id 的独立 B 树;要返回任何其它字段,引擎都必须取出完整的 "
        "BSON 文档再对其投影。一次点查就是一次这样的取出,很便宜;在单体节点文档——结构、标题、摘要、叶子正文"
        "都在一份文档里——上扫一棵大子树,则是数万次,且无论投影多小,每次都读出并解码整份文档。我们向 MongoDB "
        "提出这一点时,其产品团队确认了该读取路径,并确认新版服务端(含 8.0 SBE)不改变它。数据常驻缓存"
        "(且 WiredTiger 缓存中的页是未压缩存放的),所以代价既非磁盘 I/O 也非解压:时间花在每文档的工作上——"
        "定位记录、物化文档、应用投影、经游标传出——再乘以子树大小。其它检索操作只触达少量文档,在任何布局下都快。"),
      P("两组受控测量(单客户端、集合常驻、同一组 200 条子树路径)把代价钉在取出的<b>次数</b>而非大小上。"
        "其一,16、32、64 个后台进程对同一服务端狂打点查时,单体布局的扫描延迟几乎不动,全程零缓存逐出:"
        "代价就是扫描本身,不是争用。其二,缩小被扫描的文档没有用。把宽 {0} 字段拆出去,每份被扫文档缩小约 75%,"
        "子树延迟不变;哪怕只剩约 150 字节的纯拓扑文档,取出成本的大头仍在——定位并物化约 3.6 万份文档的开销"
        "与它们的重量无关。因此只有<b>去掉</b>取出这一步才有用,这正是候选 schema 做的事(第 5 节)。"
        .format(C('text'))),
      P(f"行式与列式引擎在每个节点上做的工更少。行存访问每一行匹配,却只物化被请求的列;宽 {C('text')} 留在行里"
        f"未被读取(PostgreSQL 甚至不解压该 JSONB datum,因为 {C('node_id')} 是独立的列)。列存只以向量化批次读取"
        "被请求的那一列。这是文档模型的取舍,也是 MongoDB 在别处取胜的原因:把一个节点的字段存在一起,让"
        "“取出整个实体”很快(点查与子节点读取)、让就地更新便宜(无 vacuum 债)、让磁盘形态紧凑。PageIndex "
        "工作负载在除大子树扫描之外的每个操作上都顺着这个模型——因此候选 schema 让这一个操作改由索引服务。")]

# 5 Candidate schema
F += [H1("5　候选 schema:结构解耦 + 键值元数据"),
      P("一个节点携带三类数据,检索在三条不同的路径上读它们,候选 schema 就把三者分开存:"),
      P(f"<b>结构</b>({C('node_id')}、{C('path')}、{C('parent_id')}、{C('depth')}):约 150 字节的拓扑文档,"
        f"带精简覆盖索引 {C('{path, node_id}')}(生产中以 {C('tree_id')} 打头),服务 {C('get_subtree')} 与导航。"
        f"<br/><b>元数据</b>({C('title')}、{C('summary')}):以 {C('(tree_id, node_id)')} 或等价 namespace 的 _id 为键。"
        f"<br/><b>正文</b>:叶子正文独立成集,使用同样的租户限定键,服务 {C('get_entity')}。"),
      figure("schema", frac=0.9),
      CAP("图 3:候选 schema。一棵子树就是小覆盖索引 {path, node_id} 上的一段连续区间,"
          "get_subtree 全程不离开索引;它返回的 node id 即元数据存储(k:v)与正文集合的键,"
          "只为实际渲染或被选中的节点读取。"),
      P(f"在此布局下,子树扫描不再触碰任何文档:{C('explain')} 确认 {C('PROJECTION_COVERED')}、"
        f"{C('totalDocsExamined: 0')},一千万节点树上 {C('get_subtree')} 为 P50 15.5 ms / P95 168 ms(表 9),"
        "这是 MongoDB 单引擎绝对值,不与其它 campaign 的 PostgreSQL 数字相除。MongoDB 产品团队提示了两个陷阱:覆盖执行是"
        f"全有或全无——索引漏掉任何被投影字段、或投影里留着 {C('_id')},每文档取出就悄悄回来(结构集合"
        "不走覆盖索引时,哪怕文档只有约 150 字节,也要慢约 1.6 倍);覆盖索引的维护成本落在写入上,"
        "而写一次的树把它变成一次性的摄入成本——先批量装载,再建索引。")]
F += [table([["读取", "P50", "P95", "说明"],
             ["锚点:去 text 单体布局,视图(FETCH)", "38.3", "421", "对齐此前实验"],
             ["锚点:去 text 单体布局,id(覆盖)", "17.3", "181", "对齐此前实验"],
             ["结构扫描,id,无覆盖索引", "25.2", "260", "每节点一次 FETCH"],
             ["结构扫描,id(覆盖)", "15.5", "168", "totalDocsExamined: 0"],
             ["+ 全部约 36k 节点元数据,1k-id 分块", "96.3", "1034", "harness:约 36 次调用"]],
            ["l", "r", "r", "l"],
            [7.2 * cm, 1.7 * cm, 1.7 * cm, 4.0 * cm], key_row=4,
            bold=[(4, 1), (4, 2)]),
      CAP("表 9:MongoDB 单引擎的一千万节点 schema 消融(单客户端,ms),不是跨引擎证据。")]
F += [P(f"ids-only 扫描不能代表完整树视图。元数据行一次解析约 3.6 万节点,约 1 秒 P95,但约 36 次调用来自 harness "
        f"固定的 1,000-id 分块,不是 16 MiB 上限;当前短 id 的完整 BSON 命令只有约 0.62 MiB。该行是机制压力测试,"
        "不是生产端到端延迟。若部署确实需要单条索引查询返回完整视图,备选是把元数据并入合并集合的覆盖索引:"
        "它仍然被覆盖、胜过单体扫描,但数 KB 的摘要键会把索引推到与数据本身相当的体积——这正是候选 schema 把"
        "元数据放进键值存储的原因。"),
      P("拆分同时让热工作集很小。一千万节点的结构集合在磁盘上是 0.28 GB 数据加 0.57 GB 索引(WiredTiger "
        "前缀压缩去重了共享的 path 前缀),元数据存储再加 0.69 GB 与 0.14 GB 的 _id 索引:导航工作集合计约 "
        "1.7 GB,而含正文的单体集合是 5.3 GB。生产规模上,多树多租户共享主机时,这一差距决定导航字节能否"
        "常驻缓存:正文永远不与结构争抢 WiredTiger 缓存。"),
      P(f"<b>更大的收益需要结构化反规范化(未实测)。</b> Nested Sets 或按树序 {C('_id')} 的 clustered "
        "collection 把子树变成一次区间查询或顺序读;预计算的子树文档把它变成一次点读。三者都因树基本写一次"
        "而可行(代价在装载时一次付清),也可能降低每键扫描成本。但我们没有测它们,因此不能声称会追平 PostgreSQL;"
        "是否值得增加 schema 复杂度必须由完整端到端测量决定。"
        f"({C('$graphLookup')} 不是候选:它重走 {C('parent_id')}、仍然物化文档,7.0 里还有 100 MB 上限且无视 "
        f"{C('allowDiskUse')}。)")]

# 6 Deployment validation plan
F += [H1("6　部署验证方案"),
      P("第 5 节的 schema 在缓存常驻的主机上测得;生产部署还要加上相反的约束——多树多租户可能让工作集大于"
        "内存,方案因此是:"),
      P(f"<b>1. 端到端验证解耦 schema。</b> 结构({C('tree_id')}、{C('node_id')}、{C('parent_id')}、{C('depth')}、"
        f"{C('path')})进导航集合;{C('title')} 与 {C('summary')} 进以 {C('(tree_id, node_id)')} 为键的元数据集合;"
        f"正文同键另存。表 9 只近似其中的 MongoDB covered scan;完整三集合读取与格式化尚未测量。"),
      P(f"<b>2. 每个索引与分片键都以 {C('tree_id')} 打头。</b> 多租户读取必须保持租户局部:点查、子节点、"
        "子树区间、取内容都应由 tree_id 打头的复合索引服务。树的数量增长时选择性不衰减,也是水平扩展的天然分片键。"),
      P(f"<b>3. 覆盖索引保持精简、并与投影严格对齐。</b> {C('{tree_id, path, node_id}')} 让子树扫描仅凭"
        "索引运行;批量装载后再建,维护成本一次付清。覆盖执行全有或全无——漏一个被投影字段、或投影留着 "
        f"{C('_id')},每文档 FETCH 就悄悄回来——所以把 {C('explain')} 显示 {C('totalDocsExamined: 0')} 当作"
        "部署检查项,而非一次性的观察。不要把数 KB 的摘要放进覆盖键:元数据存储已经按点查服务它,放进索引"
        "要付出与数据相当的索引体积。"),
      P(f"<b>4. 只有在实测出需求后才反规范化热子树视图。</b> 若 {C('get_subtree')} 仍是主要延迟来源,"
        f"用写一次的结构预计算有界的结构+摘要子树视图,或加 Nested Sets / 树序 {C('_id')} 换取区间局部性。"
        "它们能去掉覆盖扫描剩余的每键工作,但带来存储放大与更复杂的摄入。"),
      P("上线验证应复现本基准缺失的生产条件:把 WiredTiger 缓存压到单体内联布局的工作集之下、解耦导航工作集"
        "之上,对照两种布局。那能直接给拆分的缓存常驻收益定价;本基准做不到,因为它测的每种布局都已常驻缓存。")]

# 7 Conclusion
F += [H1("7　结论"),
      P(f"基准识别出一个有效的 MongoDB 优化,而不是跨引擎持平。对称 300 万节点矩阵中,覆盖索引把 MongoDB 的 "
        f"{C('get_subtree')} 从 P50/P95 2.391/19.201 ms 降至 1.291/10.692 ms;PostgreSQL 获得等价优化后为 "
        "0.297/2.732 ms,因此 MongoDB 仍慢 4.35/3.91 倍。双方都用窄结构 relation 时,MongoDB 仍慢 "
        "4.65/4.07 倍。这些只是公平的 ids-only 微基准比例,不是完整端到端比例。"),
      P("设计背后的诊断是机制性的、且经厂商确认:MongoDB 读整份文档,未覆盖的扫描按命中文档数计费,与投影"
        "大小无关,新版服务端与缓存调优都不改变它。解耦让扫描路径上只剩扫描需要的字节(覆盖索引下的结构),"
        "一千万节点的 15.5/168 ms 只保留为 MongoDB 单引擎容量点,不与旧 PostgreSQL 数字相除。剩余验证包括"
        "受限缓存 A/B,以及使用真实 formatter、租户限定键和结果边界的完整 structure/metadata/text 路径。")]

doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                        topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                        title="PageIndex 存储引擎基准(中文版)", author="Junyao Dong")
doc.build(F)
print("wrote", OUT)
