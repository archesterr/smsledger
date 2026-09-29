package app.smsledger;

import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;

/** Sending, shared by the SMS receiver, the background job and the screen. */
final class App {
    private static final int JOB_SEND = 1, JOB_CHECK = 2;

    private App() {}

    static String agent(Context ctx) {
        String version = "?";
        try {
            version = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException ignored) {
            // our own package is always there
        }
        return "smsledger-android/" + version + " (Android " + Build.VERSION.RELEASE + ")";
    }

    /** Sends what is queued; if something is left (no internet, server busy) the job tries again. */
    static Sender.Outcome flush(Context ctx, Sender.Progress progress) {
        Sender.Outcome out = send(ctx, progress);
        if (out == Sender.Outcome.RETRY) schedule(ctx);
        return out;
    }

    /** The job's part: it retries itself (jobFinished) instead of scheduling a new one. */
    static Sender.Outcome send(Context ctx, Sender.Progress progress) {
        Store store = new Store(ctx);
        if (!store.connected() || store.revoked()) return Sender.Outcome.REVOKED;
        Queue queue = store.queue();
        Sender.Outcome out = Sender.drain(queue, store.api(), (count, left) -> {
            store.addSent(count);
            if (progress != null) progress.sent(count, left);
        });
        if (out == Sender.Outcome.REVOKED) store.setRevoked();
        return out;
    }

    /** A job that runs as soon as there is internet, retried with backoff until the queue is empty. */
    static void schedule(Context ctx) {
        JobScheduler js = ctx.getSystemService(JobScheduler.class);
        ComponentName job = new ComponentName(ctx, SendJob.class);
        js.schedule(new JobInfo.Builder(JOB_SEND, job)
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setBackoffCriteria(30_000, JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .setPersisted(true)
                .build());
        if (js.getPendingJob(JOB_CHECK) == null) {
            // a safety net twice a day, in case a retry was dropped by an aggressive battery saver
            js.schedule(new JobInfo.Builder(JOB_CHECK, job)
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setPeriodic(12 * 60 * 60 * 1000L)
                    .setPersisted(true)
                    .build());
        }
    }
}
