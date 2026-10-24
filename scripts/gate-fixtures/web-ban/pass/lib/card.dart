// Colour shades and dialogs are not viewport units.
final c = Colors.blue.shade100;
void open(BuildContext ctx) => showDialog(context: ctx, builder: (_) => const Text('x')).show;
final h = MediaQuery.of(context).size.height;
// Never write 0.5.sw here; size from LayoutBuilder.
