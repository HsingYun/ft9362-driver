#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 HsingYun
"""Render documentation figures from aggregate statistics and synthetic fields.

Requires offline NumPy, SciPy and Matplotlib; no device access or biometric input.
Set IMAGING_FONT to a CJK font file if fontconfig cannot find a suitable font.
"""

import json
import os
from pathlib import Path
import subprocess

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Polygon, Rectangle
import numpy as np
from scipy.ndimage import gaussian_filter

HERE = Path(__file__).resolve().parent
DATA = json.loads((HERE / 'measurements.json').read_text())
BLUE, TEAL, ORANGE = '#245a81', '#167d8d', '#bf651c'


def configure():
    font = os.environ.get('IMAGING_FONT')
    if not font:
        font = subprocess.check_output(
            ['fc-match', '-f', '%{file}', 'sans-serif:lang=zh-cn'], text=True
        ).strip()
    font_manager.fontManager.addfont(font)
    plt.rcParams.update({
        'font.family': font_manager.FontProperties(fname=font).get_name(),
        'font.size': 10, 'axes.titlesize': 12, 'axes.labelsize': 10,
        'axes.spines.top': False, 'axes.spines.right': False,
        'axes.unicode_minus': False, 'figure.facecolor': 'white',
        'savefig.facecolor': 'white', 'axes.titlepad': 10,
    })


def save(fig, name, title, note, bottom=.07):
    fig.suptitle(title, fontsize=16, fontweight='bold', y=.98)
    fig.text(.5, .02, note, ha='center', va='bottom', fontsize=10, color='#495566')
    fig.tight_layout(rect=(.015, bottom, .985, .91), pad=1.7)
    fig.savefig(HERE / name, dpi=160)
    plt.close(fig)


def texture(x, y):
    # An arbitrary curved sinusoid: no real image, minutia or orientation input.
    phase = (x + .0025 * (y - 70)**2 + 2 * np.sin(y / 25)) / 10
    return 1600 + 400 * np.cos(2 * np.pi * phase)


def geometry():
    fig, ax = plt.subplots(1, 3, figsize=(11, 5.5), gridspec_kw={'width_ratios': [2, 1, 1.4]})
    y, x = np.mgrid[:192, :192]
    field = texture(x, y)
    ax[0].imshow(field, cmap='gray', vmin=1200, vmax=2000)
    ax[0].add_patch(Rectangle((75.5, 19.5), 40, 152, fill=False, ec=ORANGE, lw=2))
    ax[0].set_title('独立生成的曲线纹理场')
    ax[0].set_xlabel('任意像素坐标'); ax[0].set_ylabel('任意像素坐标')
    crop = field[20:172, 76:116]
    for a, im, title in [(ax[1], crop, '40 列 × 152 行'), (ax[2], crop.reshape(76, 80), '误排为 80 列 × 76 行')]:
        a.imshow(im, cmap='gray', vmin=1200, vmax=2000, interpolation='nearest')
        a.set_title(title); a.set_xlabel('列'); a.set_ylabel('行')
    save(fig, 'geometry.png', '模拟｜窄幅观测与行宽解释', '全部纹理由公式生成；图中纹路、位置与亮度不来自真实指纹。')


def photometry():
    y, x = np.mgrid[:152, :40]
    base = texture(x, y)
    mask = 1 / (1 + np.exp(np.clip((((x - 8) / 27)**2 + ((y - 70) / 64)**2 - 1) * 12, -60, 60)))
    rng = np.random.default_rng(9362)
    variants = [base, 1800 + .18 * (base - 1600), base + 5 * y,
                mask * base + (1 - mask) * 2500,
                gaussian_filter(base, 2) + rng.normal(0, 90, base.shape)]
    names = ['基准纹理', '低调制幅度', '叠加缓变背景', '局部纹理区域', '模糊与随机扰动']
    fig, axes = plt.subplots(2, 5, figsize=(11, 8))
    for j, (im, name) in enumerate(zip(variants, names)):
        for i in range(2):
            lo, hi = (1000, 2700) if i == 0 else np.percentile(im, [5, 95])
            a = axes[i, j]
            a.imshow(im, cmap='gray', vmin=lo, vmax=hi, interpolation='nearest')
            a.set_xticks([]); a.set_yticks([])
            if i == 0: a.set_title(name)
        axes[0, j].set_xlabel('固定 1000–2700')
        axes[1, j].set_xlabel('各图 P5–P95')
    axes[0, 0].set_ylabel('共同灰度映射'); axes[1, 0].set_ylabel('逐图拉伸')
    save(fig, 'photometry.png', '模拟｜显示对比度不等于采集质量', '纯数学扰动，无真实图像；参数只用于示意，不是传感器噪声、接触或压力模型。')


def histogram(ax, key, xlabel, denominator, color=BLUE):
    d = DATA[key]; edges = np.array(d['edges']); counts = np.array(d['counts'])
    ax.bar(edges[:-1], 100 * counts / denominator, width=np.diff(edges), align='edge',
           color=color, edgecolor='white', linewidth=.6)
    ax.set_xlabel(xlabel); ax.set_ylabel('占比（%）'); ax.grid(axis='y', alpha=.2)


def distributions():
    fig, ax = plt.subplots(1, 3, figsize=(12, 4.8))
    histogram(ax[0], 'capture_gray', '原始采样值（码值）', DATA['capture_frames'] * 6080)
    histogram(ax[1], 'capture_contrast', '每帧 P95−P5（码值）', DATA['capture_frames'], TEAL)
    histogram(ax[2], 'texture_support', '内区纹理支持比例', DATA['capture_frames'], ORANGE)
    ax[0].set_title('全部像素｜4,602,560 个')
    ax[1].set_title('帧内灰度跨度｜757 帧')
    ax[2].set_title('探索性纹理门限｜757 帧')
    ax[2].set_xlim(0, 1)
    save(fig, 'distributions.png', '实测汇总｜十指原始值、对比度与纹理支持', '单人、单设备、同一晚；像素和窗口存在相关性，纹理支持比例不是接触面积或质量真值。')


def scale():
    fig, ax = plt.subplots(1, 3, figsize=(11, 5), gridspec_kw={'width_ratios': [2.5, 1, 1]})
    histogram(ax[0], 'period', '候选主周期（像素）', DATA['period']['patches'])
    p5, median, p95 = DATA['period']['percentiles']
    ax[0].axvspan(p5, p95, color=TEAL, alpha=.12)
    ax[0].axvline(median, color=ORANGE, ls='--', label=f'中位数 {median:.2f}')
    ax[0].set_title('实测｜10,236 个合格窗口'); ax[0].legend(frameon=False)
    y, x = np.mgrid[:152, :40]; im = texture(x, y)
    for a, side, n in zip(ax[1:], [17, 25], [816, 512]):
        margin = (side - 1) // 2
        a.imshow(im, cmap='gray', vmin=1200, vmax=2000)
        a.add_patch(Rectangle((margin - .5, margin - .5), 40 - 2 * margin, 152 - 2 * margin,
                              ec=TEAL, fc=TEAL, alpha=.25, lw=1.5))
        a.add_patch(Rectangle((19.5 - side / 2, 75.5 - side / 2), side, side, ec=ORANGE, fill=False, lw=2))
        a.set_title(f'模拟｜{side}×{side} 邻域')
        a.set_xticks([]); a.set_yticks([])
        a.set_xlabel(f'仅卷积支持：{n} 个中心\n示意周期 10 像素')
    save(fig, 'scale.png', '实测与模拟｜纹理尺度和窄幅边界的取舍', '左：实测频谱统计。右：模拟，步长 2；未计预处理与旋转的额外边界损失。')


def rotated_rectangle(degrees):
    theta = np.deg2rad(degrees)
    rot = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    return np.array([[-20, -76], [20, -76], [20, 76], [-20, 76]]) @ rot.T


def intersection(poly):
    # Clip a convex rotated rectangle against the fixed rectangle.
    out = list(poly)
    for axis, bound, sign in [(0, -20, 1), (0, 20, -1), (1, -76, 1), (1, 76, -1)]:
        src, out = out, []
        for a, b in zip(src, src[1:] + src[:1]):
            ain, bin_ = sign * (a[axis] - bound) >= -1e-10, sign * (b[axis] - bound) >= -1e-10
            if ain != bin_:
                out.append(a + (b - a) * ((bound - a[axis]) / (b[axis] - a[axis])))
            if bin_: out.append(b)
    return np.array(out)


def area(poly):
    return abs(np.dot(poly[:, 0], np.roll(poly[:, 1], 1)) - np.dot(poly[:, 1], np.roll(poly[:, 0], 1))) / 2


def rotation():
    fig, ax = plt.subplots(1, 5, figsize=(13, 4.4), gridspec_kw={'width_ratios': [1, 1, 1, 1, 2.1]})
    for a, angle in zip(ax[:4], [0, 30, 60, 90]):
        p = rotated_rectangle(angle); shared = intersection(p)
        a.add_patch(Polygon(rotated_rectangle(0), fill=False, ec=BLUE, lw=1.5))
        a.add_patch(Polygon(p, fill=False, ec=ORANGE, lw=1.5))
        a.add_patch(Polygon(shared, color=TEAL, alpha=.4))
        a.set_xlim(-90, 90); a.set_ylim(-90, 90); a.set_aspect('equal'); a.axis('off')
        a.set_title(f'{angle}°\n重叠 {area(shared) / 6080:.1%}')
    angles = np.arange(181); ratios = [area(intersection(rotated_rectangle(a))) / 6080 for a in angles]
    assert abs(ratios[0] - 1) < 1e-10 and abs(ratios[90] - 40 / 152) < 1e-10
    ax[4].plot(angles, np.array(ratios) * 100, color=TEAL, lw=2)
    ax[4].set_xlabel('相对旋转（度）'); ax[4].set_ylabel('交集 / 单个窗口（%）')
    ax[4].set_xticks([0, 45, 90, 135, 180]); ax[4].grid(alpha=.2)
    save(fig, 'rotation.png', '几何模拟｜旋转改变共同观测区域', '理想 40×152 同中心窗口、无位移与形变；不是实测角度分布，也不是算法匹配成功率。')


def temporal():
    fig, ax = plt.subplots(1, 2, figsize=(11.5, 5), gridspec_kw={'width_ratios': [1.5, 1]})
    samples = DATA['validation']['sampling'][:6]
    ax[0].bar(range(6), [s['distinct_payloads'] for s in samples], color=[BLUE, BLUE, TEAL, TEAL, BLUE, BLUE])
    ax[0].set_xticks(range(6), ['1\n无扩展', '2\n仅状态', '3\n条件取图', '4\n条件取图', '5\n仅状态', '6\n无扩展'])
    ax[0].set_ylim(0, 11); ax[0].set_yticks([0, 1, 5, 10]); ax[0].set_ylabel('不同普通载荷数 / 每阶段 10 帧')
    ax[0].set_title('实测通道顺序｜右手拇指持续接触')
    timing = DATA['validation']['transaction_ms']
    for i, key in enumerate(['raw_frame', 'extended_frame']):
        low, mid, high = timing[key]
        ax[1].errorbar(i, mid, yerr=[[mid-low], [high-mid]], fmt='o', capsize=8, color=[BLUE, TEAL][i], lw=2)
        ax[1].text(i, high + 6, f'{mid:.2f} ms', ha='center')
    ax[1].set_xticks([0, 1], [f"普通取图\n{DATA['validation']['normal_frames']} 次", f"扩展取图\n{DATA['validation']['extended_frames']} 次"])
    ax[1].set_xlim(-.5, 1.5); ax[1].set_ylim(0, 125); ax[1].set_ylabel('OUT + IN 用时（ms）')
    ax[1].set_title('综合诊断｜中位数及 P5–P95'); ax[1].grid(axis='y', alpha=.2)
    save(fig, 'temporal.png', '实测汇总｜读取、刷新与传输耗时', '左图统计不同载荷，右图保留每次真实读取（含重复）；载荷不同不等于独立按压，传输速率不等于采样率。')


def model_canvas(title, subtitle, height=7):
    fig, ax = plt.subplots(figsize=(16, height))
    fig.subplots_adjust(left=.025, right=.975, bottom=.075, top=.85)
    ax.set(xlim=(0, 16), ylim=(0, height-1.25), aspect='equal')
    ax.axis('off')
    fig.text(.035, .94, title, fontsize=20, weight='bold', color='#172e41')
    fig.text(.035, .885, subtitle, fontsize=11, color='#627383')
    return fig, ax


def model_save(fig, name, note):
    fig.text(.035, .025, note, fontsize=10, color='#627383')
    fig.savefig(HERE / name, dpi=180)
    plt.close(fig)


def flow(ax, start, end, color='#8295a3', style='->', rad=0):
    ax.annotate('', xy=end, xytext=start, arrowprops=dict(
        arrowstyle=style, color=color, lw=1.5, connectionstyle=f'arc3,rad={rad}'))


def stage_label(ax, x, y, number, title, detail=''):
    ax.text(x, y, number, color=TEAL, fontsize=10, weight='bold')
    ax.text(x+.38, y, title, color='#172e41', fontsize=12, weight='bold')
    if detail:
        ax.text(x+.38, y-.32, detail, color='#627383', fontsize=9)


def volume(ax, x, y, side, depth, color, channels, label):
    from matplotlib.colors import to_rgb
    c=np.array(to_rgb(color)); light=tuple(.32*c+.68); mid=tuple(.60*c+.40)
    dx,dy=depth,.36*depth
    ax.add_patch(Polygon([(x,y+side),(x+dx,y+side+dy),(x+side+dx,y+side+dy),(x+side,y+side)],fc=light,ec=color,lw=.8))
    ax.add_patch(Polygon([(x+side,y),(x+side+dx,y+dy),(x+side+dx,y+side+dy),(x+side,y+side)],fc=mid,ec=color,lw=.8))
    ax.add_patch(Rectangle((x,y),side,side,fc=light,ec=color,lw=1))
    for t in np.linspace(0,1,7)[1:-1]:
        ax.plot([x+t*side,x+t*side],[y,y+side],color='white',lw=.6,alpha=.65)
        ax.plot([x,x+side],[y+t*side,y+t*side],color='white',lw=.6,alpha=.65)
    for t in np.linspace(0,depth,5)[1:-1]:
        ax.plot([x+side+t,x+side+t],[y+.36*t,y+side+.36*t],color=color,lw=.45,alpha=.45)
    ax.text(x+(side+dx)/2,y+side+dy+.18,f'{channels} ch',ha='center',color=color,fontsize=10,weight='bold')
    ax.text(x+(side+dx)/2,y-.30,label,ha='center',color='#627383',fontsize=9)


def vector(ax, x, y, width, height, phase=0, cmap='YlGnBu', n=24):
    values=.25+.7*(np.sin(np.arange(n)*1.7+phase)**2)
    ax.imshow(values[:,None],extent=(x,x+width,y,y+height),cmap=cmap,vmin=0,vmax=1,aspect='auto',interpolation='nearest',zorder=3)


def synthetic_patch(ax, cx, cy, radius, angle=0):
    from matplotlib.patches import Circle
    y,x=np.mgrid[-12:13,-12:13]
    a=np.deg2rad(angle)
    z=texture(x*np.cos(a)-y*np.sin(a),70+x*np.sin(a)+y*np.cos(a))
    rgba=plt.get_cmap('gray')((z-1200)/800);rgba[:,:,3]=(x*x+y*y<=144)
    ax.imshow(rgba,extent=(cx-radius,cx+radius,cy-radius,cy+radius),aspect='auto',zorder=3)
    ax.add_patch(Circle((cx,cy),radius,ec=TEAL,fc='none',lw=1.3,zorder=4))
    ax.plot([cx,cx+.7*radius*np.cos(a)],[cy,cy+.7*radius*np.sin(a)],color=ORANGE,lw=1.6,zorder=5)


def cnn_model():
    fig,ax=model_canvas('局部 CNN · 从圆形观测到方向归一化描述',
        '正式模型结构  /  空间尺寸逐层缩小，通道逐层增加；双向分支共享同一组卷积权重',8)
    stage_label(ax,.15,6.15,'01','方向归一化','原图圆形支持，半径 12 px')
    stage_label(ax,4.15,6.15,'02','共享编码器','每个方向独立前向；3×3 Conv + ReLU')
    stage_label(ax,12.65,6.15,'03','方向聚合','每个物理点只保存一个描述')
    synthetic_patch(ax,.80,3.55,.57,25)
    ax.text(.8,2.66,'θ',ha='center',color=ORANGE,fontsize=13)
    for i,(cx,cy,a) in enumerate([(2.95,4.35,25),(2.95,2.85,205)]):
        synthetic_patch(ax,cx,cy,.43,a)
        ax.text(cx,cy-.7,['θ','θ + 180°'][i],ha='center',fontsize=9,color='#627383')
    flow(ax,(1.4,3.55),(1.88,3.55))
    ax.text(2.95,1.77,'25 × 25 × 2\n灰度 + 圆形掩码',ha='center',fontsize=10,color=TEAL)
    xs=[4.65,6.35,8.05,9.75,11.35]
    sides=[1.12,.87,.87,.66,.66];depths=[.18,.28,.38,.48,.59]
    for i,(x,side,dep,ch,sp) in enumerate(zip(xs,sides,depths,[32,48,64,96,128],[25,13,13,7,7])):
        volume(ax,x,3.55-side/2,side,dep,TEAL if i<3 else BLUE,ch,f'{sp} × {sp}\ns = {[1,2,1,2,1][i]}')
        if i<4: flow(ax,(x+side+dep+.08,3.55),(xs[i+1]-.12,3.55))
    flow(ax,(3.95,3.55),(4.52,3.55))
    for i in range(2):
        vector(ax,13.16+i*.40,3.04,.22,1.1,phase=i*.7)
    ax.text(13.42,4.55,'2 × 128',ha='center',fontsize=11,color=BLUE)
    flow(ax,(12.69,3.55),(12.92,3.55))
    from matplotlib.patches import Circle
    ax.add_patch(Circle((14.14,3.55),.18,fc='white',ec=TEAL,lw=1.1,zorder=6))
    ax.text(14.14,3.55,'max',ha='center',va='center',fontsize=7,color=TEAL,zorder=7)
    flow(ax,(13.89,3.55),(13.97,3.55))
    flow(ax,(14.33,3.55),(14.46,3.55))
    vector(ax,14.50,2.85,.32,1.5,phase=1.5)
    ax.text(14.66,4.57,'96-D',ha='center',fontsize=12,color=TEAL,weight='bold')
    ax.text(14.85,2.37,'线性 → L2',ha='center',fontsize=9,color='#627383')
    ax.scatter([15.42],[1.90],s=300,color=ORANGE,alpha=.8)
    ax.plot([14.14,14.14],[3.35,1.90],color=ORANGE,lw=1.3)
    flow(ax,(14.14,1.90),(15.24,1.90),ORANGE)
    ax.text(15.40,1.39,'可靠性',ha='center',color=ORANGE,fontsize=10)
    ax.text(8.45,1.8,'空间平均：每支 7 × 7 × 128 → 128',ha='center',fontsize=11,color=BLUE)
    for x,t,v in [(1.0,'实际观测边界','13 px'),(5.0,'共享参数','220,817'),(9.1,'最大点数 / 帧','128'),(13.3,'描述本体 / 视图','48 KiB')]:
        ax.text(x,.83,v,fontsize=19,color='#172e41',weight='bold',ha='center')
        ax.text(x,.38,t,fontsize=10,color='#627383',ha='center')
    model_save(fig,'cnn-model.png','模型尚未训练验收；纹理与向量均为合成示意。双支解决局部轴歧义，整体姿态另按相对基准 ±30° 检查。')


def local_methods():
    from matplotlib.patches import Circle, Arc
    fig,ax=plt.subplots(1,3,figsize=(13,5.4))
    y,x=np.mgrid[-20:21,-20:21];im=texture(x,y+70)
    for a in ax:
        a.imshow(im,extent=(-20.5,20.5,20.5,-20.5),cmap='gray',vmin=1000,vmax=2200)
        a.set(xlim=(-20,20),ylim=(20,-20),aspect='equal',xticks=[],yticks=[])
    angle=np.deg2rad(45);square=np.array([[-12,-12],[12,-12],[12,12],[-12,12]])
    rot=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
    ax[0].add_patch(Polygon(square@rot.T,fc=ORANGE,alpha=.17,ec=ORANGE,lw=2))
    ax[0].add_patch(Rectangle((-12,-12),24,24,fill=False,ec=TEAL,lw=1.5,ls='--'))
    ax[0].set_title('旋转方形 · 对角线消耗短边')
    ax[0].set_xlabel('25 px 邻域转 45° 后约占 35 px\n尚未计插值和预处理')
    ax[1].add_patch(Circle((0,0),13,fill=False,ec=ORANGE,lw=1.5,ls='--'))
    ax[1].add_patch(Circle((0,0),12,fc=TEAL,alpha=.14,ec=TEAL,lw=2))
    ax[1].arrow(0,0,9,4,color=ORANGE,width=.17,head_width=1.4,length_includes_head=True)
    ax[1].add_patch(Arc((0,0),16,16,theta1=30,theta2=295,ec=TEAL,lw=2))
    ax[1].set_title('正式设计 · 圆形支持')
    ax[1].set_xlabel('半径 12 px；保守边界 13 px\n方向变化不扩大几何半径')
    points=np.array([(r*np.cos(2*np.pi*k/n),r*np.sin(2*np.pi*k/n)) for r,n in [(0,1),(2,8),(4,12),(6,16),(8,20)] for k in range(n)])
    ax[2].scatter(points[:,0],points[:,1],s=19,c=ORANGE,edgecolors='white',linewidths=.4)
    ax[2].add_patch(Circle((0,0),12,fill=False,ec=TEAL,lw=1,ls='--',alpha=.65))
    ax[2].set_title('已测 BRISK 变体 · 小环支持')
    ax[2].set_xlabel('57 点 / 半径 8 px\n有限周期内的二进制比较')
    save(fig,'local-methods.png','局部支持 · 保留观测，控制旋转边界',
         '全部纹理与几何示意独立生成；正式方案使用中图的圆形支持，右图仅解释已测小环配置。',bottom=.14)


def graph_points(ax, x, y, width, height, color, edges=False):
    points=np.array([[.12,.17],[.38,.32],[.20,.70],[.58,.82],[.80,.55],[.83,.14]])
    p=points*np.array([width,height])+[x,y]
    if edges:
        for i,j in [(0,1),(0,2),(1,2),(1,3),(1,4),(2,3),(3,4),(4,5),(1,5)]:
            ax.plot(p[[i,j],0],p[[i,j],1],color=color,alpha=.25,lw=1.2)
    ax.scatter(p[:,0],p[:,1],s=[40,80,55,65,45,75],color=color,ec='white',lw=.9,zorder=5)
    return p


def attention_model():
    fig,ax=model_canvas('稀疏对应 · 从两组局部特征到几何证据',
        '正式匹配器  /  宽度 96 · 四头 · 两个处理块；attention 生成对应，几何检查决定其可用性',7)
    for x,n,t,d in [(.1,'01','独立点集','每侧 ≤128 点 × 96 维'),(3.85,'02','上下文交互','Self → Cross → FFN，重复两块'),(9.45,'03','部分对应','双向最佳 + 无对应项'),(13.02,'04','几何验证','一对一、残差与空间分布')]:
        stage_label(ax,x,5.1,n,t,d)
    a=graph_points(ax,.25,2.75,2.3,1.25,TEAL)
    b=graph_points(ax,.25,.85,2.3,1.25,ORANGE)
    ax.text(.4,4.17,'探测',color=TEAL,fontsize=10);ax.text(.4,.62,'参考',color=ORANGE,fontsize=10)
    p=graph_points(ax,4.15,2.85,2,1.2,TEAL,True)
    q=graph_points(ax,6.55,1.00,2,1.2,ORANGE,True)
    for i in range(len(p)):
        for j in range(len(q)):
            ax.plot([p[i,0],q[j,0]],[p[i,1],q[j,1]],color=BLUE,alpha=.36 if i==j else .045,lw=1.5 if i==j else .6,zorder=1)
    flow(ax,(2.85,3.35),(4.03,3.35),TEAL)
    flow(ax,(2.85,1.5),(6.45,1.5),ORANGE,rad=.18)
    ax.text(4.25,2.45,'同图距离偏置',fontsize=9,color=TEAL)
    ax.text(6.15,3.93,'跨图上下文',fontsize=9,color=BLUE)
    ax.text(6.2,.45,'图示六点；颜色表示输入侧，不是身份标签',ha='center',fontsize=8.5,color='#627383')
    matrix=np.full((7,7),.035)
    for i,j in enumerate([0,2,1,3,6,4]):matrix[i,j]=.85
    matrix[6,5]=.8;matrix[6,6]=np.nan
    ax.imshow(matrix,extent=(9.55,12.10,1.2,3.75),cmap='YlGnBu',vmin=0,vmax=1,aspect='auto',interpolation='nearest')
    for k in range(8):
        ax.plot([9.55+k*2.55/7]*2,[1.2,3.75],color='white',lw=.6)
        ax.plot([9.55,12.1],[1.2+k*2.55/7]*2,color='white',lw=.6)
    ax.add_patch(Rectangle((12.1-2.55/7,1.2),2.55/7,2.55,fill=False,ec=ORANGE,lw=1.5))
    ax.add_patch(Rectangle((9.55,1.2),2.55,2.55/7,fill=False,ec=ORANGE,lw=1.5))
    ax.text(10.80,4.0,'对应分数',ha='center',fontsize=10,color=BLUE)
    ax.text(10.80,.81,'橙色边：允许无对应',ha='center',fontsize=9,color=ORANGE)
    flow(ax,(8.65,2.5),(9.4,2.5));flow(ax,(12.25,2.5),(13.0,2.5))
    src=np.array([[0,0],[.45,.2],[.14,.8],[.75,.95],[1.00,.5],[.75,-.2]])*.85+[13.1,2.2]
    theta=.35;r=np.array([[np.cos(theta),-np.sin(theta)],[np.sin(theta),np.cos(theta)]])
    dst=(src-[13.1,2.2])@r.T+[14.35,2.20]
    for i in range(6):
        ax.plot([src[i,0],dst[i,0]],[src[i,1],dst[i,1]],color=TEAL,lw=1.1,alpha=.60)
    ax.scatter(src[:,0],src[:,1],c=TEAL,s=30,zorder=4)
    ax.scatter(dst[:,0],dst[:,1],facecolor='white',ec=TEAL,s=40,zorder=4)
    ax.plot([src[1,0],15.40],[src[1,1],3.8],color=ORANGE,ls='--',lw=1)
    ax.scatter([15.40],[3.8],c=ORANGE,marker='x',s=60)
    ax.text(14.45,1.36,'保留一致对应\n排除几何离群',ha='center',fontsize=10,color=TEAL)
    model_save(fig,'attention-model.png','节点、矩阵和对应全部为独立构造的示意，不是模型输出；示意六点少于正式最低内点要求，不表示可认证。')


def matching_pipeline():
    fig,ax=model_canvas('FT9362 · 正式认证的数据路径',
        '观测 → 局部表示 → 上下文对应 → 身份决策；原始图像仅存在于动作工作区',7)
    for x,n,t,d in [(.1,'01','窄幅观测','40 × 152 / 独立按压'),(3.6,'02','局部表示','圆形 CNN / ≤128 点'),(7.9,'03','模板比较','attention + 刚体几何'),(12.1,'04','身份决策','八视图聚合 / 事务校准')]:
        stage_label(ax,x,5.12,n,t,d)
    y,x=np.mgrid[:152,:40]
    ax.imshow(texture(x,y),extent=(.65,1.45,1.45,4.49),cmap='gray',vmin=1000,vmax=2200,aspect='auto')
    from matplotlib.patches import Circle
    ax.add_patch(Circle((1.05,3.40),.24,fill=False,ec=ORANGE,lw=2))
    ax.plot([1.30,2.2],[3.62,3.95],color=ORANGE,lw=1)
    synthetic_patch(ax,2.50,3.55,.52,25)
    flow(ax,(3.12,3.55),(3.8,3.55))
    a=graph_points(ax,3.95,2.50,1.45,1.8,TEAL,True)
    for i in range(6):vector(ax,5.65+i*.13,2.65,.085,1.30,i*.31,n=14)
    ax.text(5.75,2.18,'坐标 + 96 维描述',ha='center',fontsize=10,color=TEAL)
    flow(ax,(6.62,3.40),(7.65,3.40))
    src=graph_points(ax,7.83,2.88,1.3,1.3,TEAL)
    dst=graph_points(ax,9.55,2.88,1.3,1.3,ORANGE)
    for i in range(6):ax.plot([src[i,0],dst[i,0]],[src[i,1],dst[i,1]],color=TEAL,alpha=.4,lw=1)
    ax.text(9.3,2.40,'逐参考视图核对共同结构',ha='center',fontsize=10,color='#627383')
    for i in range(8):
        xx=7.80+i*.40;yy=.55+i*.035
        ax.add_patch(Rectangle((xx,yy),.65,.95,fc='#f2f6f8',ec=BLUE,lw=.7))
        ax.scatter([xx+.15,xx+.4,xx+.3],[yy+.25,yy+.5,yy+.72],s=6,c=TEAL)
    ax.text(9.42,.13,'仅存八视图特征；约 404 KiB / 身份',ha='center',fontsize=9,color=BLUE)
    flow(ax,(9.4,1.83),(9.4,2.65),ORANGE)
    flow(ax,(11.08,3.40),(12.03,3.40))
    scores=[.34,.75,.48,.58,.88,.39,.52,.64]
    for i,s in enumerate(scores):
        ax.add_patch(Rectangle((12.2+i*.29,2.85),.18,s*1.18,fc=TEAL if i==4 else '#bed9de',ec='none'))
    ax.text(13.34,2.44,'身份内取最高有效视图',ha='center',fontsize=10,color=TEAL)
    ax.plot([12.18,14.52],[4.0,4.0],ls='--',color=ORANGE,lw=1)
    ax.text(14.7,3.99,'阈值',va='center',fontsize=9,color=ORANGE)
    ax.text(13.35,1.55,'匹配 / 未匹配 / 重试 / 错误',ha='center',fontsize=11,color='#172e41')
    ax.text(13.35,.85,'最多十指图库\n最多三次按压 / 事务；±30°',ha='center',fontsize=10,color='#627383')
    model_save(fig,'matching-pipeline.png','合成纹理、点集及分数仅用于解释信息流，不代表真实指纹、模型效果或已完成的身份认证。')


def processing_evidence():
    e=DATA['extended_characteristics']
    fig,ax=plt.subplots(1,3,figsize=(16,5.7),gridspec_kw={'width_ratios':[1.2,1,1]})
    h=e['fixed_pixel']['capture_residual_histogram'];edges=np.array(h['edges']);counts=np.array(h['counts'])
    colors=[ORANGE if left<=0<right else BLUE for left,right in zip(edges[:-1],edges[1:])]
    ax[0].bar((edges[:-1]+edges[1:])/2,counts,width=np.diff(edges)*.84,color=colors)
    ax[0].set(xlabel='目标值 − 上下左右均值（码值）',ylabel='唯一图像数',xlim=(-15,280))
    ax[0].set_title('固定坐标的两种数值行为',loc='left')
    ax[0].annotate('139 帧精确为 0',xy=(5,139),xytext=(25,145),fontsize=10,color=ORANGE,arrowprops={'arrowstyle':'-','color':ORANGE})
    ax[0].text(180,145,'其余 618 帧均为正\n中位数 187.5',ha='center',fontsize=10,color=BLUE)
    ax[0].set_ylim(0,165);ax[0].grid(axis='y',alpha=.13)
    fixed=DATA['duplicate_sensitivity']['fixed_coordinate']
    keys=['D1_comparison/raw_frame','D1_movement/raw_frame','D2/raw_frame']
    for i,key in enumerate(keys):
        row=fixed[key]
        a=row['record_weighted']['equals_fraction']*100
        b=row['unique_payload_weighted']['equals_fraction']*100
        ax[1].plot([a,b],[i,i],color='#bed0d8',lw=3,zorder=1)
        ax[1].scatter(a,i,s=50,color=ORANGE,label='每次读取等权' if i==0 else None,zorder=2)
        ax[1].scatter(b,i,s=50,color=TEAL,label='每种内容等权' if i==0 else None,zorder=2)
        ax[1].text(a,i+.16,f'{a:.1f}%',ha='center',va='top',fontsize=9,color=ORANGE)
        ax[1].text(b,i-.16,f'{b:.1f}%',ha='center',va='bottom',fontsize=9,color=TEAL)
    ax[1].set(yticks=[0,1,2],yticklabels=['D1 对照','D1 动作','D2 综合'],xlim=(0,100),ylim=(2.65,-.55),xlabel='精确等于四邻域均值的比例（%）')
    ax[1].set_title('普通通道｜重复权重的影响',loc='left')
    ax[1].legend(loc='lower right',fontsize=8,frameon=False);ax[1].grid(axis='x',alpha=.13)
    p=e['analysis']['photometry_and_background'];u=e['followup']['high_raw_correlation']
    values=[p['high_pairs_raw_correlation'],p['high_pairs_detrended_correlation'],u['local_normalized_correlation']]
    for i,(lo,mid,hi) in enumerate(values):
        ax[2].errorbar(mid,i,xerr=[[mid-lo],[hi-mid]],fmt='o',ms=9,lw=2.7,capsize=5,color=[BLUE,'#718eaa',TEAL][i])
        ax[2].text(mid,i-.21,f'{mid:.3f}',ha='center',fontsize=11,color=[BLUE,'#718eaa',TEAL][i])
    ax[2].set(yticks=[0,1,2],yticklabels=['原图','去平面','局部归一化'],xlim=(0,1),ylim=(2.5,-.5),xlabel='Pearson 相关系数')
    ax[2].set_title('同一组 119 个高相关异指对',loc='left');ax[2].grid(axis='x',alpha=.13)
    save(fig,'processing-evidence.png','实测汇总｜处理痕迹与背景相似性',
         '左图为十指唯一图像；中图比例不是固件触发概率；右图为选定异指对的中位数及 P5–P95，不代表匹配性能。',bottom=.11)


def finger_spectrum():
    g=DATA['extended_characteristics']['analysis']['ungated_spectral_analysis']['groups']
    fig,ax=plt.subplots(1,3,figsize=(12.8,5.2))
    metrics=[('detrended_std','局部调制幅度','去平面标准差（码值）',1),
             ('spectral_concentration','频谱集中程度','峰邻域 / 指定频带功率',1),
             ('high_to_ridge_energy','相对高频分量','高频 / 指定频带功率（%）',100)]
    for a,(key,title,ylab,scale_factor) in zip(ax,metrics):
        for i,(group,color) in enumerate([('little',ORANGE),('other',TEAL)]):
            lo,mid,hi=np.array(g[group][key])*scale_factor
            a.errorbar(i,mid,yerr=[[mid-lo],[hi-mid]],fmt='o',color=color,ms=10,lw=2.5,capsize=7)
            a.annotate(f'{mid:.2f}',(i,mid),xytext=(12,0),textcoords='offset points',va='center',color=color,fontsize=11)
        a.set(xlim=(-.5,1.8),xticks=[0,1],xticklabels=['两根小指\n145 帧','其余八指\n612 帧'],ylabel=ylab)
        a.set_ylim(bottom=0);a.set_title(title,loc='left');a.grid(axis='y',alpha=.13)
    save(fig,'finger-spectrum.png','实测汇总｜不预筛选窗口的小指差异',
         '每帧先汇总 16 窗口，再统计组内中位数和 P5–P95；高频比例不是噪声测量，分组差异不等于生理因果。',bottom=.13)


if __name__ == '__main__':
    configure()
    for draw in (geometry, photometry, distributions, scale, rotation, temporal,
                 cnn_model, local_methods, attention_model, matching_pipeline,
                 processing_evidence, finger_spectrum):
        draw()
