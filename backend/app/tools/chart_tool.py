"""
高级数据可视化工具 - Data Visualization Specialist

支持图表类型：
- 柱状图 (bar): 分类对比
- 折线图 (line): 趋势分析
- 饼图 (pie): 占比分析
- 散点图 (scatter): 关系探索
- 直方图 (histogram): 单变量分布
- 箱线图 (boxplot): 统计概览
- 热力图 (heatmap): 相关性分析
- 面积图 (area): 累积趋势

遵循可视化最佳实践：
1. 意图识别 → 图表选型 → 数据预处理 → 视觉设计 → 代码生成
2. 强制中文支持
3. 不编造数据，明确声明模拟数据
4. 严禁滥用 3D 图和过度装饰
"""

import io
import base64
import json
import os
import re
from pathlib import Path
from typing import Optional, Dict, List, Union
import pandas as pd
import numpy as np

# matplotlib 字体/配置缓存：~/.matplotlib 常不可写（容器/CI），
# 改用项目内目录，避免每次启动重建缓存（拖慢 5-10 秒）。必须在本模块 import matplotlib 之前设置。
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[2] / "data" / ".matplotlib"))

# matplotlib + seaborn (静态图)
import matplotlib
matplotlib.use('Agg')  # 无GUI后端：避免 macOS 下后台线程创建 NSWindow 导致进程崩溃
import matplotlib.pyplot as plt
import seaborn as sns

# plotly (交互图，可选)
try:
    import plotly.express as px
    import plotly.graph_objects as go
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

# LangChain tool 装饰器
from langchain_core.tools import tool
from app.core.process_executor import LocalToolProcessError, LocalToolTimeoutError, run_in_process
from app.core.timeouts import LOCAL_TOOL_TIMEOUT_SECONDS


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────
# 中文支持与样式配置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────

def _setup_chinese():
    """配置中文字体，确保图表正常显示中文。"""
    plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS', 'DejaVu Sans', 'Microsoft YaHei']
    plt.rcParams['axes.unicode_minus'] = False
    plt.rcParams['figure.figsize'] = (10, 6)
    plt.rcParams['axes.labelsize'] = 12
    plt.rcParams['xtick.labelsize'] = 10
    plt.rcParams['ytick.labelsize'] = 10
    plt.rcParams['legend.fontsize'] = 10
    plt.rcParams['figure.titlesize'] = 14


_setup_chinese()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────
# 工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────

def _validate_data(data: List[Dict]) -> pd.DataFrame:
    """将用户数据转换为 DataFrame，验证完整性。"""
    df = pd.DataFrame(data)
    
    if df.empty:
        raise ValueError("数据为空，无法生成图表")
    
    # 检查是否有至少一列数值
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    obj_cols = df.select_dtypes(include=['object']).columns.tolist()
    
    if not num_cols and len(obj_cols) < 2:
        raise ValueError("数据格式不正确：至少需要一列标签列和一列数值列")
    
    return df


def _auto_select_chart_type(df: pd.DataFrame, intent: str = "auto") -> str:
    """
    根据数据特征和用户意图自动推荐图表类型。
    
    Args:
        df: 输入 DataFrame
        intent: 用户意图 ('trend', 'compare', 'distribution', 'relationship', 'composition')
    
    Returns:
        推荐的图表类型
    """
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    obj_cols = df.select_dtypes(include=['object']).columns.tolist()
    
    if intent == "auto":
        # 自动检测数据特征
        if len(num_cols) >= 2 and len(df) > 10:
            # 多数值列，可能适合散点图或热力图
            if len(df) < 100:
                return "scatter"
            else:
                return "heatmap"
        elif len(obj_cols) >= 1 and len(num_cols) >= 1:
            # 分类 + 数值，适合柱状图或饼图
            unique_count = df[obj_cols[0]].nunique()
            if unique_count > 10:
                return "bar"  # 分类太多用柱状图
            else:
                return "pie"  # 分类少可用饼图
        elif len(num_cols) == 1:
            return "histogram"  # 单数值列用直方图
    
    return intent


def _generate_bar_chart(df: pd.DataFrame, title: str = "数据可视化", **kwargs) -> bytes:
    """生成柱状图。"""
    fig, ax = plt.subplots(figsize=(12, 6))
    
    # 自动选择列
    obj_cols = df.select_dtypes(include=['object']).columns.tolist()
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    
    if not obj_cols or not num_cols:
        raise ValueError("柱状图需要至少一列标签和一列数值")
    
    x_col = obj_cols[0]
    y_col = num_cols[0]
    
    # 绘图
    bars = ax.bar(df[x_col].astype(str), df[y_col], color='#1890ff', alpha=0.8, edgecolor='white')
    
    # 添加数值标签
    for bar, val in zip(bars, df[y_col]):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + max(df[y_col]) * 0.01,
                f'{val:.1f}', ha='center', va='bottom', fontsize=9)
    
    ax.set_xlabel(x_col, fontsize=12)
    ax.set_ylabel(y_col, fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.grid(axis='y', alpha=0.3, linestyle='--')
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_line_chart(df: pd.DataFrame, title: str = "趋势分析", **kwargs) -> bytes:
    """生成折线图。"""
    fig, ax = plt.subplots(figsize=(12, 6))
    
    obj_cols = df.select_dtypes(include=['object']).columns.tolist()
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    
    if not obj_cols or not num_cols:
        raise ValueError("折线图需要至少一列标签和至少一列数值")
    
    x_col = obj_cols[0]
    
    # 绘制所有数值列
    colors = ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1']
    for idx, y_col in enumerate(num_cols):
        color = colors[idx % len(colors)]
        ax.plot(df[x_col].astype(str), df[y_col], marker='o', linewidth=2, 
                markersize=4, label=y_col, color=color)
    
    ax.set_xlabel(x_col, fontsize=12)
    ax.set_ylabel('值', fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.legend(loc='best')
    ax.grid(alpha=0.3, linestyle='--')
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_pie_chart(df: pd.DataFrame, title: str = "占比分析", **kwargs) -> bytes:
    """生成饼图。"""
    obj_cols = df.select_dtypes(include=['object']).columns.tolist()
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    
    if not obj_cols or not num_cols:
        raise ValueError("饼图需要至少一列标签和一列数值")
    
    x_col = obj_cols[0]
    y_col = num_cols[0]
    
    # 饼图数据
    labels = df[x_col].astype(str).tolist()
    sizes = df[y_col].tolist()
    
    # 颜色方案（柔和色）
    colors = ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1', 
              '#13c2c2', '#eb2f96', '#fa8c16', '#a0d911', '#2f54eb']
    
    fig, ax = plt.subplots(figsize=(10, 8))
    
    wedges, texts, autotexts = ax.pie(
        sizes, 
        labels=labels, 
        autopct='%1.1f%%',
        colors=colors[:len(labels)],
        startangle=90,
        pctdistance=0.85,
        wedgeprops=dict(width=0.5, edgecolor='white')
    )
    
    ax.set_title(title, fontsize=14, fontweight='bold')
    plt.setp(texts, fontsize=11)
    plt.setp(autotexts, fontsize=10, fontweight='bold')
    ax.axis('equal')  # 保证饼图是圆形
    
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_scatter_plot(df: pd.DataFrame, title: str = "关系探索", **kwargs) -> bytes:
    """生成散点图。"""
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    
    if len(num_cols) < 2:
        raise ValueError("散点图需要至少两列数值数据")
    
    x_col = num_cols[0]
    y_col = num_cols[1]
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    # 如果有第三列可用于颜色编码
    if len(num_cols) >= 3 and 'color_col' in kwargs:
        color_col = kwargs['color_col']
        scatter = ax.scatter(df[x_col], df[y_col], c=df[color_col], 
                            cmap='viridis', alpha=0.6, edgecolors='k', s=100)
        plt.colorbar(scatter, label=color_col)
    else:
        ax.scatter(df[x_col], df[y_col], alpha=0.6, edgecolors='k', 
                  s=100, color='#1890ff')
    
    ax.set_xlabel(x_col, fontsize=12)
    ax.set_ylabel(y_col, fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.grid(alpha=0.3, linestyle='--')
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_histogram(df: pd.DataFrame, title: str = "数据分布", **kwargs) -> bytes:
    """生成直方图。"""
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    
    if not num_cols:
        raise ValueError("直方图需要至少一列数值数据")
    
    col = num_cols[0]
    bins = kwargs.get('bins', 30)
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    sns.histplot(df[col].dropna(), bins=bins, kde=True, ax=ax, 
                 color='#1890ff', stat='density', alpha=0.7)
    
    ax.set_xlabel(col, fontsize=12)
    ax.set_ylabel('频率', fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.grid(alpha=0.3, linestyle='--')
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_boxplot(df: pd.DataFrame, title: str = "统计概览", **kwargs) -> bytes:
    """生成箱线图。"""
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    obj_cols = df.select_dtypes(include=['object']).columns.tolist()
    
    if not num_cols:
        raise ValueError("箱线图需要至少一列数值数据")
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    if obj_cols:
        # 按分类变量分组
        group_col = obj_cols[0]
        data_to_plot = [df[df[group_col] == group][num_cols[0]].dropna() 
                       for group in df[group_col].unique()]
        bp = ax.boxplot(data_to_plot, labels=[str(g) for g in df[group_col].unique()],
                       patch_artist=True, showmeans=True)
        
        # 美化
        for patch, color in zip(bp['boxes'], ['#1890ff', '#52c41a', '#faad14', '#f5222d']):
            patch.set_facecolor(color)
            patch.set_alpha(0.7)
    else:
        # 简单箱线图
        sns.boxplot(y=num_cols[0], data=df, ax=ax, color='#1890ff', alpha=0.7)
    
    ax.set_xlabel(obj_cols[0] if obj_cols else '', fontsize=12)
    ax.set_ylabel(num_cols[0], fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.grid(axis='y', alpha=0.3, linestyle='--')
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_area_chart(df: pd.DataFrame, title: str = "累积趋势", **kwargs) -> bytes:
    """生成面积图。"""
    obj_cols = df.select_dtypes(include=['object']).columns.tolist()
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    
    if not obj_cols or not num_cols:
        raise ValueError("面积图需要至少一列标签和至少一列数值")
    
    x_col = obj_cols[0]
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    # 堆叠面积图
    colors = ['#1890ff', '#52c41a', '#faad14', '#f5222d', '#722ed1']
    
    if len(num_cols) == 1:
        # 单列面积图
        ax.fill_between(range(len(df)), df[num_cols[0]], alpha=0.3, color=colors[0])
        ax.plot(range(len(df)), df[num_cols[0]], color=colors[0], linewidth=2)
    else:
        # 多列堆叠
        for idx, col in enumerate(num_cols):
            color = colors[idx % len(colors)]
            ax.fill_between(range(len(df)), df[col], alpha=0.3, color=color, label=col)
    
    ax.set_xlabel(x_col, fontsize=12)
    ax.set_ylabel('值', fontsize=12)
    ax.set_title(title, fontsize=14, fontweight='bold')
    ax.legend(loc='upper right')
    ax.grid(alpha=0.3, linestyle='--')
    plt.xticks(range(len(df)), [str(v) for v in df[x_col]], rotation=45, ha='right')
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _generate_heatmap(df: pd.DataFrame, title: str = "相关性热力图", **kwargs) -> bytes:
    """生成热力图。"""
    # 提取数值列
    num_df = df.select_dtypes(include=[np.number])
    
    if num_df.shape[1] < 2:
        raise ValueError("热力图需要至少两列数值数据")
    
    # 计算相关矩阵
    corr_matrix = num_df.corr()
    
    fig, ax = plt.subplots(figsize=(10, 8))
    
    mask = np.triu(np.ones_like(corr_matrix, dtype=bool))
    sns.heatmap(corr_matrix, mask=mask, annot=True, fmt='.2f', cmap='coolwarm',
                center=0, square=True, linewidths=1, ax=ax,
                cbar_kws={"shrink": .8}, annot_kws={"size": 10})
    
    ax.set_title(title, fontsize=14, fontweight='bold')
    plt.tight_layout()
    
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────
# 主工具函数
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━────────────

# 图表类型映射
CHART_GENERATORS = {
    'bar': _generate_bar_chart,
    'line': _generate_line_chart,
    'pie': _generate_pie_chart,
    'scatter': _generate_scatter_plot,
    'histogram': _generate_histogram,
    'boxplot': _generate_boxplot,
    'area': _generate_area_chart,
    'heatmap': _generate_heatmap,
}


def _generate_chart_payload(
    chart_type: str,
    title: str,
    data: List[Dict],
    x_column: Optional[str] = None,
    y_column: Optional[str] = None,
    **kwargs
) -> str:
    """
    🎨 高级数据可视化工具 - 生成高质量静态图表
    
    支持的图表类型：
    - bar: 柱状图（分类对比）
    - line: 折线图（趋势分析）
    - pie: 饼图（占比分析）
    - scatter: 散点图（关系探索）
    - histogram: 直方图（分布分析）
    - boxplot: 箱线图（统计概览）
    - area: 面积图（累积趋势）
    - heatmap: 热力图（相关性分析）
    
    参数：
    - chart_type: 图表类型（必填）
    - title: 图表标题（必填）
    - data: 数据列表，格式：[{"列名1": 值1, "列名2": 值2}, ...]（必填）
    - x_column: X轴列名（可选，自动检测）
    - y_column: Y轴列名（可选，自动检测）
    - bins: 直方图的bin数（仅histogram使用）
    - color_col: 颜色编码列（仅scatter使用）
    
    示例：
    data = [{"科目": "数学", "人数": 30}, {"科目": "英语", "人数": 25}]
    generate_chart("bar", "各科及格人数", data)
    """
    try:
        # 验证数据
        df = _validate_data(data)
        
        # 确定图表类型
        ct = (chart_type or 'bar').lower()
        if ct not in CHART_GENERATORS:
            raise ValueError(f"不支持的图表类型：{ct}。支持的类型：{', '.join(CHART_GENERATORS.keys())}")
        
        # 调用生成器
        generator = CHART_GENERATORS[ct]
        image_bytes = generator(df, title=title, x_column=x_column, y_column=y_column, **kwargs)
        
        # 转换为 base64 用于展示
        base64_str = base64.b64encode(image_bytes).decode('utf-8')
        
        # 生成 ECharts JSON（供前端渲染）
        obj_cols = df.select_dtypes(include=['object']).columns.tolist()
        num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        
        echarts_option = None
        if ct == 'bar' and obj_cols and num_cols:
            echarts_option = {
                "title": {"text": title},
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": df[obj_cols[0]].astype(str).tolist()},
                "yAxis": {"type": "value"},
                "series": [{"type": "bar", "name": num_cols[0], "data": df[num_cols[0]].tolist()}]
            }
        elif ct == 'line' and obj_cols and num_cols:
            series = []
            for col in num_cols:
                series.append({"name": col, "type": "line", "data": df[col].tolist()})
            echarts_option = {
                "title": {"text": title},
                "tooltip": {"trigger": "axis"},
                "xAxis": {"type": "category", "data": df[obj_cols[0]].astype(str).tolist()},
                "yAxis": {"type": "value"},
                "series": series
            }
        elif ct == 'pie' and obj_cols and num_cols:
            pie_data = [{"name": n, "value": v} for n, v in zip(df[obj_cols[0]], df[num_cols[0]])]
            echarts_option = {
                "title": {"text": title},
                "tooltip": {"trigger": "item"},
                "legend": {},
                "series": [{"type": "pie", "radius": "60%", "data": pie_data}]
            }
        
        result = f"<!--CHART:{json.dumps(echarts_option or {}, ensure_ascii=False)}-->"
        result += f"\n<!--IMAGE_BASE64:{base64_str}-->"
        result += f"\n✅ 图表已生成：{title}（{ct.upper()}）"
        result += "\n💡 提示：前端可同时渲染 ECharts 交互图和静态图片"
        
        return result
        
    except Exception as e:
        return f"❌ 图表生成失败：{str(e)}\n请检查数据格式和参数设置。"


def _auto_analyze_and_visualize_payload(data: List[Dict], intent: str = "auto") -> str:
    """
    🤖 智能可视化 - 自动分析数据并选择最佳图表类型
    
    功能：
    1. 自动识别数据类型（趋势/对比/分布/关系/构成）
    2. 推荐最合适的图表类型
    3. 生成专业级图表
    
    参数：
    - data: 数据列表，格式：[{"列名1": 值1, "列名2": 值2}, ...]
    - intent: 用户意图（可选）
      * "trend": 趋势分析 → 折线图/面积图
      * "compare": 对比分析 → 柱状图
      * "distribution": 分布分析 → 直方图/箱线图
      * "relationship": 关系分析 → 散点图/热力图
      * "composition": 构成分析 → 饼图/堆叠柱状图
      * "auto": 自动检测（默认）
    
    示例：
    data = [
        {"月份": "1月", "销售额": 120},
        {"月份": "2月", "销售额": 150},
        ...
    ]
    auto_analyze_and_visualize(data, intent="trend")
    """
    try:
        df = _validate_data(data)
        
        # 自动选择图表类型
        if intent == "auto":
            chart_type = _auto_select_chart_type(df)
        else:
            chart_type = intent
        
        # 生成图表
        generator = CHART_GENERATORS.get(chart_type)
        if not generator:
            supported = ', '.join(CHART_GENERATORS.keys())
            return f"❌ 不支持的意图：{intent}。支持的意图：{supported}"
        
        title = f"{chart_type.upper()} - 数据洞察"
        image_bytes = generator(df, title=title)
        base64_str = base64.b64encode(image_bytes).decode('utf-8')
        
        # 生成统计摘要
        num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        stats = {}
        for col in num_cols:
            stats[col] = {
                "平均值": f"{df[col].mean():.2f}",
                "最大值": f"{df[col].max():.2f}",
                "最小值": f"{df[col].min():.2f}",
                "中位数": f"{df[col].median():.2f}"
            }
        
        stats_json = json.dumps(stats, ensure_ascii=False, default=str)
        
        result = f"<!--CHART:auto:{chart_type}-->"
        result += f"\n<!--IMAGE_BASE64:{base64_str}-->"
        result += f"\n<!--STATS:{stats_json}-->"
        result += f"\n✅ 智能分析完成：已选择 {chart_type} 类型展示数据"
        result += f"\n📊 数据统计摘要：{stats_json}"
        
        return result
        
    except Exception as e:
        return f"❌ 智能分析失败：{str(e)}"


def _run_chart_process(operation: str, fn, *args, **kwargs) -> str:
    try:
        return run_in_process(
            fn,
            *args,
            timeout_seconds=LOCAL_TOOL_TIMEOUT_SECONDS,
            **kwargs,
        )
    except LocalToolTimeoutError:
        return (
            f"[TOOL_TIMEOUT] {operation} 本地图表生成超时。"
            "请减少数据量、简化图表类型后重试。"
        )
    except LocalToolProcessError as exc:
        return f"❌ 图表生成进程异常：{exc}"


@tool
def generate_chart(
    chart_type: str,
    title: str,
    data: List[Dict],
    x_column: Optional[str] = None,
    y_column: Optional[str] = None,
    **kwargs
) -> str:
    """生成高质量静态图表（独立进程执行，超时自动回收）。"""
    return _run_chart_process(
        "generate_chart",
        _generate_chart_payload,
        chart_type,
        title,
        data,
        x_column,
        y_column,
        **kwargs,
    )


@tool
def auto_analyze_and_visualize(data: List[Dict], intent: str = "auto") -> str:
    """自动选择图表类型并生成静态图（独立进程执行，超时自动回收）。"""
    return _run_chart_process(
        "auto_analyze_and_visualize",
        _auto_analyze_and_visualize_payload,
        data,
        intent,
    )


# 导出工具列表
def get_chart_tools():
    """返回所有可视化工具。"""
    return [generate_chart, auto_analyze_and_visualize]
