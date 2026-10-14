// AdsKiller.m v2 — SimpleQR (qrcode 4.0.3) 去广告注入 dylib
//
// 纯 Objective-C runtime swizzling，不依赖 CydiaSubstrate / Theos 运行时。
//
// v2 相对 v1 的变化（v1 注入后横幅照旧，针对可能的原因逐个补上）：
//   1. 挂载点从「只有 constructor」改为「+load + constructor + 延时重试」，
//      避免个别类在 constructor 时还没注册导致整批 hook 静默失效；
//   2. 新增 autoload 路径拦截（-setAdUnitID: / -setAutoloadEnabled:）——
//      App 若用 autoload，SDK 自己加载，根本不会走 -loadRequest:；
//   3. 新增最终落地方法 -loadAd:responseInfo:error:completionHandler: 拦截；
//   4. 新增 Pangle 横幅（PAGBannerAd / PAGNativeExpressBannerView）与
//      Vungle 横幅（VungleBannerView）的加载入口；
//   5. 启动日志带版本号与「装了几个 hook / 几个类没找到」，便于判断 dylib 是否真的生效。

#import <Foundation/Foundation.h>
#import <UIKit/UIKit.h>
#import <objc/runtime.h>
#import <dispatch/dispatch.h>

#define AK_VERSION "v2"

#define AK_LOG(fmt, ...) NSLog(@"[AdsKiller] " fmt, ##__VA_ARGS__)

// ---------- 可配置开关 ----------
// 1 = 启动时调 AdMob 官方 API 关闭聚合中介初始化（Pangle/Vungle/Moloco/InMobi 适配器不再初始化）
#define AK_DISABLE_MEDIATION_INIT 1
// 1 = 加载被拦截时回调 delegate 的失败方法，让宿主 App 自己收起横幅高度
#define AK_NOTIFY_BANNER_FAILURE 1
// 1 = 连 AdMob SDK 初始化也拦掉（更彻底；个别 App 拿到 nil 状态会崩，默认关闭）
#define AK_KILL_SDK_INIT 0
// 1 = 把横幅视图 frame 高度置 0（宿主不收起横幅时的兜底）
#define AK_COLLAPSE_BANNER_VIEW 0

// ---------- 协议契约（ARC 下对 id 发未声明 selector 会编译报错） ----------
@protocol AKGADMobileAdsShim <NSObject>
+ (instancetype)sharedInstance;
- (void)disableMediationInitialization;
@end

@protocol AKBannerDelegateShim <NSObject>
- (void)bannerView:(id)bannerView didFailToReceiveAdWithError:(NSError *)error;
@end

// ---------- 统计 ----------
static int gHooks = 0;
static int gMissed = 0;

// ---------- runtime 工具 ----------

static void AKSwizzleClassMethod(Class cls, SEL sel, IMP newImp) {
    if (!cls || !sel) return;
    Method m = class_getClassMethod(cls, sel);
    if (!m) { AK_LOG(@"  x class method not found: +[%@ %@]", cls, NSStringFromSelector(sel)); return; }
    method_setImplementation(m, newImp);
    gHooks++;
    AK_LOG(@"  + hooked +[%@ %@]", cls, NSStringFromSelector(sel));
}

static void AKSwizzleInstanceMethod(Class cls, SEL sel, IMP newImp) {
    if (!cls || !sel) return;
    Method m = class_getInstanceMethod(cls, sel);
    if (!m) return;                       // 类里没这个方法就静默跳过（大量可选目标）
    method_setImplementation(m, newImp);
    gHooks++;
    AK_LOG(@"  + hooked -[%@ %@]", cls, NSStringFromSelector(sel));
}

static Class AKClass(NSString *name) {
    Class c = NSClassFromString(name);
    if (!c) { gMissed++; AK_LOG(@"  ? class not present: %@", name); }
    return c;
}

static NSError *AKFakeError(void) {
    return [NSError errorWithDomain:@"com.google.ads" code:0 userInfo:nil];
}

// 让宿主 App 走“加载失败”分支（它自己的 delegate 会收起横幅高度）
static void AKNotifyBannerFailure(id bannerView) {
#if AK_NOTIFY_BANNER_FAILURE
    @try {
        id<AKBannerDelegateShim> delegate =
            (id<AKBannerDelegateShim>)[bannerView valueForKey:@"delegate"];
        SEL sel = @selector(bannerView:didFailToReceiveAdWithError:);
        if (delegate && [delegate respondsToSelector:sel]) {
            [delegate bannerView:bannerView didFailToReceiveAdWithError:AKFakeError()];
        }
    } @catch (NSException *e) { AK_LOG(@"notify banner failure: %@", e); }
#endif
#if AK_COLLAPSE_BANNER_VIEW
    @try {
        if ([bannerView isKindOfClass:[UIView class]]) {
            UIView *v = (UIView *)bannerView;
            CGRect f = v.frame; f.size.height = 0; v.frame = f;
        }
    } @catch (NSException *e) {}
#endif
}

// ======================= AdMob (GAD) =======================

static void AKLoadFullScreenAd(id self, SEL _cmd, id adUnitID, id request, void (^completion)(id, NSError *)) {
    AK_LOG(@"BLOCK %@ %@", NSStringFromClass([self class]), NSStringFromSelector(_cmd));
    if (completion) completion(nil, AKFakeError());
}

static void AKPresent(id self, SEL _cmd, UIViewController *vc) {
    AK_LOG(@"BLOCK present: %@", NSStringFromClass([self class]));
}

static void AKPresentRewarded(id self, SEL _cmd, UIViewController *vc, id handler) {
    AK_LOG(@"BLOCK rewarded present: %@", NSStringFromClass([self class]));
}

static void AKLoadRequest(id self, SEL _cmd, id request) {
    AK_LOG(@"BLOCK %@ on %@", NSStringFromSelector(_cmd), NSStringFromClass([self class]));
    if ([self isKindOfClass:[UIView class]]) AKNotifyBannerFailure(self);
}

// 最终落地方法：不管请求从哪来（loadRequest: / autoload / 中介），都会走到这里
static void AKLoadAdFinal(id self, SEL _cmd, id ad, id responseInfo, id err, id completion) {
    AK_LOG(@"BLOCK %@ on %@", NSStringFromSelector(_cmd), NSStringFromClass([self class]));
    if ([self isKindOfClass:[UIView class]]) AKNotifyBannerFailure(self);
}

static void AKLoadResponseString(id self, SEL _cmd, id s) {
    AK_LOG(@"BLOCK %@ on %@", NSStringFromSelector(_cmd), NSStringFromClass([self class]));
    if ([self isKindOfClass:[UIView class]]) AKNotifyBannerFailure(self);
}

// autoload 路径：广告位 ID 直接丢掉，SDK 无从加载
static void AKSetAdUnitID(id self, SEL _cmd, id adUnitID) {
    AK_LOG(@"BLOCK setAdUnitID: (%@) on %@", adUnitID, NSStringFromClass([self class]));
}

static void AKSetAutoload(id self, SEL _cmd, BOOL enabled) {
    AK_LOG(@"BLOCK setAutoloadEnabled:%d on %@", enabled, NSStringFromClass([self class]));
}

static id AKPreloadedAd(id self, SEL _cmd, id adUnitID) {
    return nil;
}

static void AKMobileAdsStart(id self, SEL _cmd, void (^completion)(id)) {
    AK_LOG(@"BLOCK -[GADMobileAds startWithCompletionHandler:]");
    if (completion) completion(nil);
}

// ======================= Vungle =======================

static void AKVungleBlock(id self, SEL _cmd, id arg) {
    AK_LOG(@"BLOCK Vungle %@ %@", NSStringFromClass([self class]), NSStringFromSelector(_cmd));
}

// ======================= Pangle / PAG =======================

static void AKPangleShow(id self, SEL _cmd, UIViewController *vc) {
    AK_LOG(@"BLOCK Pangle show: %@", NSStringFromClass([self class]));
}

static void AKPangleShowRit(id self, SEL _cmd, UIViewController *vc, id a, id b) {
    AK_LOG(@"BLOCK Pangle show(rit): %@", NSStringFromClass([self class]));
}

static void AKPangleRegisterContainer(id self, SEL _cmd, id container, id clickableViews) {
    AK_LOG(@"BLOCK Pangle native registerContainer:withClickableViews:");
}

static void AKPangleLoadNative(id self, SEL _cmd, id count) {
    AK_LOG(@"BLOCK Pangle native %@ on %@", NSStringFromSelector(_cmd), NSStringFromClass([self class]));
}

static void AKPangleBannerLoad(id self, SEL _cmd, id slotID, id request, void (^completion)(id, NSError *)) {
    AK_LOG(@"BLOCK Pangle banner load: %@", NSStringFromSelector(_cmd));
    if (completion) completion(nil, AKFakeError());
}

static void AKPangleLoadAdData(id self, SEL _cmd) {
    AK_LOG(@"BLOCK Pangle banner %@ on %@", NSStringFromSelector(_cmd), NSStringFromClass([self class]));
}

// ======================= 装配 =======================

static void AKInstallHooks(void) {
    @autoreleasepool {
        gHooks = 0;
        gMissed = 0;

        // ---- AdMob：全屏广告加载入口 ----
        Class appOpen = AKClass(@"GADAppOpenAd");
        Class interstitial = AKClass(@"GADInterstitialAd");
        Class rewarded = AKClass(@"GADRewardedAd");

        SEL loadSel = NSSelectorFromString(@"loadWithAdUnitID:request:completionHandler:");
        AKSwizzleClassMethod(appOpen, loadSel, (IMP)AKLoadFullScreenAd);
        AKSwizzleClassMethod(interstitial, loadSel, (IMP)AKLoadFullScreenAd);
        AKSwizzleClassMethod(rewarded, loadSel, (IMP)AKLoadFullScreenAd);
        AKSwizzleClassMethod(interstitial,
            NSSelectorFromString(@"loadFullScreenAdWithAdUnitID:request:completionHandler:"),
            (IMP)AKLoadFullScreenAd);

        SEL preloadedSel = NSSelectorFromString(@"preloadedAdWithAdUnitID:");
        AKSwizzleClassMethod(appOpen, preloadedSel, (IMP)AKPreloadedAd);
        AKSwizzleClassMethod(interstitial, preloadedSel, (IMP)AKPreloadedAd);
        AKSwizzleClassMethod(rewarded, preloadedSel, (IMP)AKPreloadedAd);

        // ---- AdMob：展示入口（兜底）----
        SEL presentSel = NSSelectorFromString(@"presentFromRootViewController:");
        AKSwizzleInstanceMethod(appOpen, presentSel, (IMP)AKPresent);
        AKSwizzleInstanceMethod(interstitial, presentSel, (IMP)AKPresent);
        AKSwizzleInstanceMethod(rewarded,
            NSSelectorFromString(@"presentFromRootViewController:userDidEarnRewardHandler:"),
            (IMP)AKPresentRewarded);

        // ---- AdMob：横幅 / 原生（含 autoload 路径）----
        Class banner = AKClass(@"GADBannerView");
        Class adLoader = AKClass(@"GADAdLoader");
        SEL loadReq = NSSelectorFromString(@"loadRequest:");
        AKSwizzleInstanceMethod(banner, loadReq, (IMP)AKLoadRequest);
        AKSwizzleInstanceMethod(adLoader, loadReq, (IMP)AKLoadRequest);
        AKSwizzleInstanceMethod(banner, NSSelectorFromString(@"loadWithTargeting:"), (IMP)AKLoadRequest);
        AKSwizzleInstanceMethod(banner, NSSelectorFromString(@"loadWithAdResponseString:"), (IMP)AKLoadResponseString);
        AKSwizzleInstanceMethod(adLoader, NSSelectorFromString(@"loadRequestWithTarget:"), (IMP)AKLoadRequest);
        AKSwizzleInstanceMethod(adLoader, NSSelectorFromString(@"loadWithAdResponseString:"), (IMP)AKLoadResponseString);
        // autoload：把广告位 ID / 自动加载开关都变成空操作 → SDK 无从加载
        AKSwizzleInstanceMethod(banner, NSSelectorFromString(@"setAdUnitID:"), (IMP)AKSetAdUnitID);
        AKSwizzleInstanceMethod(banner, NSSelectorFromString(@"setAutoloadEnabled:"), (IMP)AKSetAutoload);
        // 最终落地方法
        AKSwizzleInstanceMethod(banner, NSSelectorFromString(@"loadAd:responseInfo:error:completionHandler:"), (IMP)AKLoadAdFinal);
        AKSwizzleInstanceMethod(adLoader, NSSelectorFromString(@"loadAd:responseInfo:error:completionHandler:"), (IMP)AKLoadAdFinal);

#if AK_KILL_SDK_INIT
        AKSwizzleInstanceMethod(AKClass(@"GADMobileAds"),
            NSSelectorFromString(@"startWithCompletionHandler:"), (IMP)AKMobileAdsStart);
#endif

        // ---- Vungle（含横幅）----
        for (NSString *n in @[@"_TtC12VungleAdsSDK18VungleInterstitial",
                              @"_TtC12VungleAdsSDK14VungleRewarded",
                              @"_TtC12VungleAdsSDK12VungleBanner",
                              @"_TtC12VungleAdsSDK16VungleBannerView",
                              @"_TtC12VungleAdsSDK12BasePublicAd"]) {
            Class c = AKClass(n);
            if (!c) continue;
            AKSwizzleInstanceMethod(c, NSSelectorFromString(@"presentWith:"), (IMP)AKVungleBlock);
            AKSwizzleInstanceMethod(c, NSSelectorFromString(@"presentOn:"), (IMP)AKVungleBlock);
            AKSwizzleInstanceMethod(c, NSSelectorFromString(@"load:"), (IMP)AKVungleBlock);
        }

        // ---- Pangle ----
        AKSwizzleInstanceMethod(AKClass(@"PAGInterfacePangleInterstitialAdapter"),
            NSSelectorFromString(@"presentFromRootViewController:"), (IMP)AKPangleShow);
        for (NSString *n in @[@"PAGLInterstitialAd", @"PAGBannerAd"]) {
            AKSwizzleInstanceMethod(AKClass(n), NSSelectorFromString(@"presentFromRootViewController:"), (IMP)AKPangleShow);
        }
        // 横幅加载（v1 漏掉的就是这里）
        Class pagBanner = AKClass(@"PAGBannerAd");
        AKSwizzleInstanceMethod(pagBanner,
            NSSelectorFromString(@"_loadAdWithSlotID:request:completionHandler:"), (IMP)AKPangleBannerLoad);
        AKSwizzleClassMethod(pagBanner,
            NSSelectorFromString(@"loadAdWithSlotID:request:completionHandler:"), (IMP)AKPangleBannerLoad);
        Class pagExpress = AKClass(@"PAGNativeExpressBannerView");
        AKSwizzleInstanceMethod(pagExpress, NSSelectorFromString(@"loadAdData"), (IMP)AKPangleLoadAdData);
        AKSwizzleInstanceMethod(pagExpress, NSSelectorFromString(@"_loadAdDataByPublisher:"), (IMP)AKVungleBlock);
        // 原生 / 信息流
        AKSwizzleInstanceMethod(AKClass(@"PAGLNativeAd"),
            NSSelectorFromString(@"registerContainer:withClickableViews:"), (IMP)AKPangleRegisterContainer);
        Class pagNativeMgr = AKClass(@"PAGNativeAdsManager");
        AKSwizzleInstanceMethod(pagNativeMgr, NSSelectorFromString(@"loadAdDataWithCount:"), (IMP)AKPangleLoadNative);
        AKSwizzleInstanceMethod(pagNativeMgr,
            NSSelectorFromString(@"loadAdDataWithCount:invokeByPublisher:"), (IMP)AKPangleLoadNative);
        Class pagRewarded = AKClass(@"PAGNativeExpressRewardedVideoAd");
        AKSwizzleInstanceMethod(pagRewarded, NSSelectorFromString(@"showAdFromRootViewController:"), (IMP)AKPangleShow);
        AKSwizzleInstanceMethod(pagRewarded,
            NSSelectorFromString(@"showAdFromRootViewController:ritScene:ritSceneDescribe:"), (IMP)AKPangleShowRit);

        AK_LOG(@"=== %s installed %d hooks, %d classes missing ===", AK_VERSION, gHooks, gMissed);
    }
}

#if AK_DISABLE_MEDIATION_INIT
static void AKDisableMediation(void) {
    @try {
        Class mobileAds = AKClass(@"GADMobileAds");
        if (mobileAds) {
            id shared = [(id<AKGADMobileAdsShim>)mobileAds sharedInstance];
            if ([shared respondsToSelector:@selector(disableMediationInitialization)]) {
                [(id<AKGADMobileAdsShim>)shared disableMediationInitialization];
                AK_LOG(@"mediation initialization disabled via GADMobileAds");
            }
        }
    } @catch (NSException *e) { AK_LOG(@"disable mediation failed: %@", e); }
}
#endif

static BOOL gInstalled = NO;

// +load 是比 constructor 更早且更可靠的挂载点：
// dyld 调用 map_images 注册完所有镜像的类之后才会执行 +load，
// 所以这里 NSClassFromString 一定能拿到 App / SDK 的类。
@interface AdsKillerLoader : NSObject @end

@implementation AdsKillerLoader
+ (void)load {
    AK_LOG(@"=== AdsKiller %s loading (+load) ===", AK_VERSION);
    gInstalled = YES;
    AKInstallHooks();
    // 有类没找到就先记着，留给 constructor / 延时重试再补一次
}
@end

__attribute__((constructor))
static void AdsKillerCtor(void) {
    AK_LOG(@"=== AdsKiller %s (constructor) ===", AK_VERSION);
    if (!gInstalled) { gInstalled = YES; AKInstallHooks(); }
    else if (gMissed > 0) {
        AK_LOG(@"retrying: %d classes were missing at +load time", gMissed);
        AKInstallHooks();
    }
    // 主线程下一轮再补一次，并关闭聚合中介初始化
    dispatch_async(dispatch_get_main_queue(), ^{
        if (gMissed > 0) {
            AK_LOG(@"retrying on main queue: %d classes still missing", gMissed);
            AKInstallHooks();
        }
#if AK_DISABLE_MEDIATION_INIT
        AKDisableMediation();
#endif
        AK_LOG(@"=== AdsKiller %s ready (hooks=%d) ===", AK_VERSION, gHooks);
    });
}
