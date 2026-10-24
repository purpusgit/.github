final pages = [
  GetPage(
    name: '/home',
    page: () => const HomeScreen(),
  ),
];
void openSettings() => Get.to(() => SettingsScreen());
void openSplash(ctx) => Navigator.push(ctx, MaterialPageRoute(builder: (_) => SplashScreen()));
