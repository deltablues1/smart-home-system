package hr.jarvis.launcher;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.widget.TextView;

/**
 * Opens any installed app on request, so Jarvis can too.
 *
 * The TCL only launches an app when handed a URI that some installed app has
 * claimed through an intent filter. A1 Xplore TV claims none — its manifest has
 * MAIN/LEANBACK_LAUNCHER and nothing else — so no link, package name or intent
 * string reaches it. This app claims one, and forwards.
 *
 *   remote.turn_on(activity="jarvis://open?pkg=hr.a1.android.tv.xploretv")
 *      -> Android resolves jarvis:// to this activity
 *      -> this activity starts A1 Xplore and gets out of the way
 *
 * Nothing here is specific to A1: any package the TV has installed works.
 */
public class LaunchActivity extends Activity {

    private static final String TAG = "JarvisLauncher";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Uri data = getIntent() != null ? getIntent().getData() : null;
        if (data == null) {
            // Opened from the TV's own app list rather than by a link: show
            // what this thing is, so it is not a mystery icon a year from now.
            showMessage("Jarvis Launcher\n\nOtvara aplikacije na zahtjev.\n"
                    + "Koristi se preko poveznice jarvis://open?pkg=<paket>");
            return;
        }

        String target = data.getQueryParameter("pkg");
        if (target == null || target.trim().length() == 0) {
            Log.w(TAG, "No pkg parameter in " + data);
            showMessage("Nedostaje ?pkg=<paket> u poveznici.");
            return;
        }

        launch(target.trim());
    }

    private void launch(String target) {
        // On Android TV the usual entry point is CATEGORY_LEANBACK_LAUNCHER, and
        // getLaunchIntentForPackage does not look for it. Try both before
        // concluding the app is unreachable.
        Intent launchIntent = getPackageManager().getLaunchIntentForPackage(target);
        if (launchIntent == null) {
            launchIntent = getPackageManager().getLeanbackLaunchIntentForPackage(target);
        }
        if (launchIntent == null) {
            // Not installed, or hidden from the launcher. Say so on screen —
            // the caller cannot see why nothing happened otherwise.
            Log.w(TAG, "No launch intent for " + target);
            showMessage("Aplikacija nije pronađena:\n" + target);
            return;
        }

        // A fresh task, otherwise the launched app ends up stacked on top of
        // this one and BACK returns here instead of leaving the app.
        launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                | Intent.FLAG_ACTIVITY_CLEAR_TOP);

        Log.i(TAG, "Launching " + target);
        startActivity(launchIntent);
        finish();   // leave no trace in the back stack
    }

    private void showMessage(String text) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setTextSize(22);
        view.setPadding(64, 64, 64, 64);
        setContentView(view);
    }
}
