# SimpleQR (qrcode 4.0.3) 去广告 — 方案 C：注入 dylib hook

针对 `qrcode-4.0.3.ipa`（Bundle: `SimpleQR`，arm64，主二进制**已解密** `cryptid=0`）的去广告实现。
所有 hook 目标都是从主二进制里**实测提取**的，不是猜的（依据见 `../analysis/`）。

---

## 1. 这个 App 的广告构成（实测结论）

| SDK | 形态 | 证据 |
|---|---|---|
| Google AdMob (`GAD*`, 394 个类) | **静态链接进主二进制** | `__objc_classlist` 中有 `GADMobileAds`/`GADBannerView`/… |
| 穿山甲 Pangle (`PAG*`) | 静态链接 + `PAGAdSDK.bundle` | `PAGSdk`、`PAGBannerAd`、`PAGLNativeAd`、`PAGLInterstitialAd`、`PAGInterfacePangle*Adapter` |
| Vungle | 静态链接 Swift 类 + `VungleAds.bundle` | `_TtC12VungleAdsSDK18VungleInterstitial` 等 |
| Moloco / InMobi | 独立 framework | `Frameworks/MolocoSDK.framework`、`InMobiSDK.framework` |
| 聚合 | **AdMob mediation** | `PAGInterfacePangle*Adapter`、GAD 中介适配器类 |

**没有内购去广告**：App 自身类里没有任何购买/订阅管理类（StoreKit 只是被 SDK 内部链接，
`_TtC8SimpleQR12ReviewHelper` 用的是 SKStoreProductViewController 评分弹窗）。
所以只能从「拦住广告加载/展示」下手。

App 自己管理广告的类（Swift，ObjC 可见）：

| 类 | 与广告相关的成员 |
|---|---|
| `_TtC8SimpleQR24CodeDetailViewController` | `adView`、`topAdView`、`rectangleAdsFooterView`、`reloadBanner` |
| `_TtC8SimpleQR25HistoryListViewController` | `adView`、`reloadBanner`、`bannerView:didFailToReceiveAdWithError:` |
| `_TtC8SimpleQR26ReaderCameraViewController` | `adView`、`bannerView:didFailToReceiveAdWithError:` |
| `_TtC8SimpleQR21WebPageViewController` | `adView` |
| `_TtC8SimpleQR26CodeDetailRectangleAdsCell` | `rectangleAdsView` |

关键点：这几个 VC **自己实现了 `bannerView:didFailToReceiveAdWithError:`**，
也就是说「加载失败 → 自己收起横幅高度」这条链路是现成的。
所以 hook 只要让**加载必然失败**，UI 就会自动收干净，不需要我们改布局。

---

## 2. 交付内容

```
tweak/
├── AdsKiller.m                  # hook dylib 源码（纯 ObjC runtime，不依赖 substrate/Theos）
├── build_dylib.sh               # macOS 上构建（只需 Xcode CLT）
├── package_trollfools.sh        # ★ 打包成 TrollFools 注入插件（.deb + .dylib + .zip）
├── inject_and_repack.sh         # 另一条路：直接注入 + 重签 + 重新打包 IPA
├── trollfools/                  # TrollFools 包骨架
│   ├── DEBIAN/control           #   deb 元信息
│   └── Library/MobileSubstrate/DynamicLibraries/AdsKiller.plist   # 过滤 com.sawadaru.qr
├── tools/mkdeb.py               # 跨平台 deb 打包器（不依赖 dpkg-deb / ar）
└── README.md
```

仓库根的 `.github/workflows/build-tweak.yml` 可以在**没有 Mac** 的情况下，
用 GitHub Actions 的 macOS runner 编译打包，直接下载构件。

### 拦截层次（从「根」到「兜底」）

1. **加载层**（主要手段）
   - `+[GADAppOpenAd|GADInterstitialAd|GADRewardedAd loadWithAdUnitID:request:completionHandler:]` → 立刻回调 `(nil, error)`
   - `+[GADInterstitialAd loadFullScreenAdWithAdUnitID:request:completionHandler:]`
   - `+[… preloadedAdWithAdUnitID:]` → 恒 `nil`
   - `-[GADBannerView loadRequest:]` / `-[GADAdLoader loadRequest:]`
   - `-[GADBannerView loadWithTargeting:]` / `loadWithAdResponseString:`、`-[GADAdLoader loadRequestWithTarget:]`
2. **展示层**（兜底）
   - `-[GADAppOpenAd|GADInterstitialAd presentFromRootViewController:]`
   - `-[GADRewardedAd presentFromRootViewController:userDidEarnRewardHandler:]`
3. **中介层**
   - 启动时调 AdMob 官方 API `-[GADMobileAds disableMediationInitialization]`（Pangle/Vungle/Moloco/InMobi 适配器不再初始化）
4. **直投兜底**（万一 App 绕过 AdMob 直接用了某家 SDK）
   - Vungle：`-[…VungleInterstitial/VungleRewarded presentWith:]`、`-[…VungleBanner presentOn:]`、`-[…BasePublicAd load:]`
   - Pangle：`-[PAGInterfacePangleInterstitialAdapter presentFromRootViewController:]`、`-[PAGLInterstitialAd/PAGBannerAd presentFromRootViewController:]`、
     `-[PAGNativeExpressRewardedVideoAd showAdFromRootViewController:…]`、`-[PAGLNativeAd registerContainer:withClickableViews:]`、`-[PAGNativeAdsManager loadAdDataWithCount:…]`

   注：`PAGLNativeAd` 实测**没有** `presentFromRootViewController:`，原生广告是通过
   `registerContainer:withClickableViews:` 把视图注册进容器渲染的，所以要拦这个方法。
   代码里所有 hook 都有 `class_getInstanceMethod` 存在性判断，缺失的会自动跳过并打日志。

横幅被拦时还会主动回调 delegate 的 `bannerView:didFailToReceiveAdWithError:`（`AK_NOTIFY_BANNER_FAILURE=1`），
让宿主 App 走自己的收起逻辑。

---

## 3. 构建 & 安装步骤

### 路线 A（推荐）：TrollFools 注入插件

不需要解包/改 IPA，也不用重签整个包 —— TrollFools 会把 App 复制一份就地注入。

**A1. 构建并打包**
```bash
cd tweak
chmod +x build_dylib.sh package_trollfools.sh
./package_trollfools.sh
```
产出在 `tweak/dist/`：

| 文件 | 用途 |
|---|---|
| `AdsKiller.deb` | MobileSubstrate 标准布局，TrollFools 推荐格式 |
| `AdsKiller.dylib` | 裸 dylib，TrollFools 里也可直接选它 |
| `AdsKiller-trollfools.zip` | 上面两个的压缩包，方便传手机 |

> 需要 Xcode Command Line Tools：`xcode-select --install`
> `ldid` 可选：`brew install ldid`（不装也行，TrollFools 注入时会自己重签）

**A1'. 没有 Mac？** 把仓库推到 GitHub，跑 `.github/workflows/build-tweak.yml`
（免费 macOS runner 编译），在 Actions 页面下载 `AdsKiller-trollfools` 构件即可。

**A2. 传到 iPhone 并注入**
1. `AdsKiller-trollfools.zip`（或直接 `AdsKiller.deb`）传到手机（AirDrop / 文件 App / 微信文件传输）。
2. 解压后，在**文件 App** 里长按 `AdsKiller.deb` → 共享 → 用 **TrollFools** 打开；
   也可以在 TrollFools 里点「+」直接选 `.dylib`。
3. 在 TrollFools 列表里找到 **QR Code / SimpleQR**（`com.sawadaru.qr`），点注入。
4. 从 TrollFools 里启动 App；如果 App 已在运行，先杀掉再启动。

**A3. 验证**：见下面「步骤 5」。

> 小知识：我们的 dylib 用的是 `__attribute__((constructor))` + ObjC runtime，
> **不依赖 CydiaSubstrate**，所以注入到没越狱的 TrollStore App 里也能正常工作。
> `trollfools/Library/.../AdsKiller.plist` 里的 `Filter.Bundles = com.sawadaru.qr`
> 是给支持过滤的注入器用的，TrollFools 忽略它也无副作用。

---

### 路线 B：改 IPA（不用 TrollFools）

### 步骤 1：解包原始 IPA（保留换行/权限）
```bash
mkdir decrypted && cd decrypted
unzip -q ../qrcode-4.0.3.ipa
# 解包后目录结构：Payload/SimpleQR.app/...
```

### 步骤 2：在 macOS 上构建 dylib
```bash
cd tweak
chmod +x build_dylib.sh inject_and_repack.sh
./build_dylib.sh        # 产出 AdsKiller.dylib (arm64)
```

### 步骤 3：注入 + 重签 + 打包
```bash
# 依赖 insert_dylib（或 optool）
brew install insert_dylib 2>/dev/null || \
  (git clone https://github.com/Tyilo/insert_dylib && cd insert_dylib && make && sudo cp insert_dylib /usr/local/bin/)

./inject_and_repack.sh ../decrypted ./AdsKiller.dylib SimpleQR-no-ads.ipa
```

### 步骤 4：安装到自己的设备
- **TrollStore**：直接把 ipa 丢进去装，自动处理签名。
- **Sideloadly / AltStore**：用自己的 Apple ID 证书装（会整包重签）。

### 步骤 5：验证
连接设备看日志（Console.app 搜 `AdsKiller`；没 Mac 也可以用 TrollFools 自带的日志/或 `idevicesyslog`），应能看到：
```
[AdsKiller] === AdsKiller ready ===
[AdsKiller] hooked +[GADAppOpenAd loadWithAdUnitID:request:completionHandler:]
[AdsKiller] blocked loadRequest: on GADBannerView
```
没有 `hooked` 输出的类说明该 SDK 版本里 selector 不同 —— 日志会打印 `method not found`，照着改 `AdsKiller.m` 即可。

---

## 3.5 注入后没效果怎么排查

**第一步永远是看日志**。启动后（Console.app / `idevicesyslog` / TrollFools 的日志）搜 `AdsKiller`，按结果分三种情况：

| 日志现象 | 含义 | 处理 |
|---|---|---|
| **完全没有 `[AdsKiller]` 字样** | dylib 根本没被加载 | TrollFools 注入没成功。改注入**裸 `.dylib`**（不要用 `.deb`），并**从 TrollFools 里启动 App**；确认注入时选的是 `com.sawadaru.qr` |
| 有 `=== AdsKiller v2 loading (+load) ===` 但 `hooks=0` 或大量 `class not present` | 加载了但没找到目标类 | 说明加载太早/类名变了。v2 有 +load + constructor + 延时三重挂载，正常不该出现；把日志发出来 |
| 有 `installed N hooks`，打开页面也**有** `BLOCK ...` 输出，但广告还在 | 命中了对的类但漏了调用路径 | 日志里的 `BLOCK` 行会显示是哪个类在加载，把它补进 hook 列表 |

**关键排障逻辑**：如果 dylib 被注入但文件损坏/架构不对，App 会**直接闪退**。
所以「App 能正常打开」通常意味着：要么 dylib 已生效（问题在 hook 覆盖面），要么压根没注入。

App 的横幅可能的**加载路径**（v1 只覆盖了第一条，v2 全cover）：

1. 显式 `-[GADBannerView loadRequest:]` —— v1 已拦
2. **autoload**：设了 `adUnitID` + `autoloadEnabled`，SDK 自己加载，**根本不走 `loadRequest:`** —— v2 新增拦截 `-setAdUnitID:` / `-setAutoloadEnabled:`
3. **Pangle 横幅**（`PAGBannerAd` / `PAGNativeExpressBannerView`）与 **Vungle 横幅**（`VungleBannerView`）—— v2 新增
4. 中介层最终落地方法 `-loadAd:responseInfo:error:completionHandler:` —— v2 新增（不管请求从哪来都会经过）

---

### 路线 C：让 CI 直接产出注入后的 IPA（不用本地工具链）

用 `.github/workflows/build-patched-ipa.yml`（GitHub Actions 的 macOS runner）在云端
解包 → 注入 → 重签 → 打包：

1. **把原始 IPA 传上去**：仓库 → Releases → Draft a new release → tag 填 `base-ipa` →
   把 `qrcode-4.0.3.ipa` 拖进附件 → Publish。
   （**不要** `git add` 提交它，`.gitignore` 已排除）
2. Actions → **build-patched-ipa** → Run workflow。也可以用 `base_ipa_url` 指定别的私密地址。
3. 跑完在 Artifacts 下载 **SimpleQR-noads-ipa**，就是已注入并重签好的 IPA。

内部步骤：解包 → `ldid -e` 保存原 entitlements（**必须在注入前取**，注入会把旧签名整个剥掉；
砸壳包签名常常是坏的、`ldid -e` 会崩，所以失败时用仓库预置的 `tweak/entitlements.plist` 兜底）→
`tweak/tools/inject_dylib.py` 用 LIEF 插入 `LC_LOAD_DYLIB` 并**剥掉失效的旧代码签名**
→ `ldid -S` 重签（失败自动退到 `codesign -f -s -`）→ 重新打包 → 校验产物。

> ⚠️ **踩过的坑**：砸壳 IPA 的主程序里那份旧签名已经失效，**不剥掉的话 ldid 重签时会直接断言崩溃**
> （报 `ldid.cpp(852): _assert(): false`）。所以注入后一定要 `remove_signature()`，
> 让 ldid 面对一个干净的未签名二进制——这也是为什么 `ldid -S` 在我们自己编的 dylib 上一直是好的。

> 注入流水线有一个**不需要原始 IPA 的自测**：`.github/workflows/selftest-injector.yml`
> 会合成一个假的 arm64 App + 用真的 AdsKiller.dylib 跑完整注入+校验+打包。

> ⚠️ **这个仓库目前是 public**。原始 IPA 和改过的 IPA 一旦作为 release 资产放上去，
> 就等于**公开分发**这份第三方 App。要走路线 C，建议先把仓库改成 private
> （Settings → General → Danger Zone → Change visibility），并且不要打开 `publish_release`。
> 只给自己设备用的话，路线 A（TrollFools 注入）其实更省事，也不涉及分发。

---

## 4. 开关与调参（`AdsKiller.m` 顶部）

| 宏 | 默认 | 作用 |
|---|---|---|
| `AK_DISABLE_MEDIATION_INIT` | 1 | 关闭 AdMob 中介初始化（省流量、少一次网络请求） |
| `AK_NOTIFY_BANNER_FAILURE` | 1 | 拦截加载时回调 delegate 失败方法，让 App 自己收起横幅 |
| `AK_KILL_SDK_INIT` | 0 | 连 `-[GADMobileAds startWithCompletionHandler:]` 也拦掉。**更彻底但风险高**（App 若解包初始化状态会崩），默认不开 |
| `AK_COLLAPSE_BANNER_VIEW` | 0 | 宿主没收起横幅时，强行把横幅 frame 高度置 0（会留空白但至少没广告） |

**排障顺序**
1. 还有广告 → 看 Console 里 `AdsKiller` 日志，确认是被哪个类弹的（日志会显示 `blocked … on <类名>`），把那个类补进 hook 列表。
2. 崩溃 → 关掉 `AK_DISABLE_MEDIATION_INIT`，再试 `AK_KILL_SDK_INIT=0`。
3. 横幅位置留空白 → 把 `AK_COLLAPSE_BANNER_VIEW` 打开。
4. App 起不来/闪退在 dylib → 检查 `install_name` 是否为 `@executable_path/AdsKiller.dylib`，且 dylib 确实拷进了 `.app`。

---

## 5. 备选方案（如果不想动包）

**方案 1：网络层屏蔽**（不改包、不重签，效果也不错）
在路由器 / iOS 私有 DNS（AdGuard、NextDNS）里屏蔽：

```
pagead2.googlesyndication.com
googleads.g.doubleclick.net
googleadservices.com
*.vungle.com
api.vungle.com
*.moloco.com
*.inmobi.com
*.pangolin-sdk.com
*.csjplatform.com
```

**方案 2：直接 patch 主二进制**（不注入 dylib）
`SimpleQR` 已解密，`__TEXT` 可写：把 `+[GADMobileAds startWithCompletionHandler:]`、
`-[GADBannerView loadRequest:]` 等入口的第一条指令改成 `ret`（ARM64 `0xC0035FD6`）。
比注入 dylib 少一个文件，但改错了容易崩，且不好回退。

---

## 6. 说明

- 改动仅用于你自己设备上的个人使用；原始 App 的版权归其作者，不要分发改过的包。
- App 每次更新（换版本）后 selector 可能变化，需重新按第 5 节流程核对。
- 本仓库里的 `analysis/` 是逆向得到的类/方法清单，可复现（脚本见 `analysis/tools/`）。
