# SimpleQR 4.0.3 二进制逆向分析产物

对象：`extracted/Payload/SimpleQR.app/SimpleQR`（arm64，主程序，`cryptid = 0` 已解密）

## 产物

| 文件 | 内容 |
|---|---|
| `objc_classes.json` | 全部 **1982 个类** → 实例方法（`name` / `super` / `methods` / `cls_va` / `ro_va`） |
| `objc_cats.json` | 23 个 category 及其方法 |
| `ad_hook_targets.txt` | 广告相关类/方法汇总（App 自身 Swift 类、AdMob、Vungle、Pangle、Moloco、InMobi、内购相关） |
| `tools/objc_classdump2.py` | **可用**的 classdump（含指针解码，解析 1982/1982） |
| `tools/query_targets.py` | 从 `objc_classes.json` 里按关键词查询目标 |
| `tools/objc_classdump.py`、`tools/decode_fixups_classdump.py`、`tools/probe_fixup_layout.py` | 探索过程中的中间脚本（保留作参考，`decode_fixups_classdump.py` 的布局判断是错的，见下） |

## 复现方式

```bash
cd extracted/Payload/SimpleQR.app
python -I ../../../analysis/tools/objc_classdump2.py      # 生成 objc_classes.json / objc_cats.json
python -I ../../../analysis/tools/query_targets.py        # 生成 ad_hook_targets.txt
```
依赖：`lief`（`pip install lief`）

## 关键技术点：这个 dump 包里的指针是「chained fixup 编码」

主二进制是**已解密**的 iOS 包（`Dump_GBLW` dump 出来的），但 `__DATA`/`__DATA_CONST` 里的指针
**不是明文地址**，而是 dyld chained fixups 的编码值。直接按普通指针读会全部解错（这是最容易卡住的地方）。

实测规律（arm64, `pointer_format = 2` / `DYLD_CHAINED_PTR_64`）：

```
bit  0..42  target        —— 明文 VM 地址（本镜像 < 2^43，无需再加基址）
bit 43..50  high8         —— 0
bit 51..61  next          —— 链上下一个 fixup 槽的偏移（8 字节为单位）
bit 62      bind          —— 0 = rebase
```

因此解码就是：

```python
def P(v):   return v & 0x7FFFFFFFFFF          # 指针字段 → 真实 VM 地址
def PA(v):  return (v & 0x7FFFFFFFFFF) & ~0x7 # 结构体指针：低 3 位是标志位，需对齐
```

`PA()` 是必须的：例如 Swift 类的 `class_t.classro` 原始值 `0x40000100d9c04a`，
真实 `class_ro` 在 `0x100d9c048`（低 2 位是标志）—— 不对齐就会读到错位数据，
表现为「1869/1982 个类能解析，App 自己的 113 个 Swift 类全都解不出来」。
对齐后 **1982/1982 全部解析成功**。

验证锚点（用于确认解码正确）：
- `__objc_classlist` 每个槽解码后必须落在 `__objc_data`（`0x100e9daf8`+，`0x28dd8`）内；
- `__objc_selrefs` 每个槽解码后必须落在 `__objc_methname`（`0x100b8fe94`+，`0xb5a93`）内。

## 其他实测事实

- 主二进制静态链接了 AdMob（394 个 `GAD*` 类），Pangle（`PAG*`）、Vungle（`_TtC12VungleAdsSDK*`）也是静态链接；
  Moloco、InMobi 是独立 framework（`Frameworks/` 下）。
- App 自身只有 **26 个 Swift 类**（`_TtC8SimpleQR*`），广告逻辑直接写在 VC 里
  （`reloadBanner`、`adView`、`topAdView`、`rectangleAdsFooterView`），
  **没有**独立的广告管理类，也**没有**内购去广告（无购买/订阅管理类）。
- 三个 VC 自己实现了 `bannerView:didFailToReceiveAdWithError:` →
  「加载失败就收起横幅高度」的逻辑是现成的，所以 hook 只需让加载必失败。
