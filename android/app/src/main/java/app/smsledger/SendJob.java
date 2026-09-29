package app.smsledger;

import android.app.job.JobParameters;
import android.app.job.JobService;

/** Runs when there is internet: sends the queue, and asks to be retried if the server was unreachable. */
public class SendJob extends JobService {
    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            boolean retry = new Store(this).queue().size() > 0
                    && App.send(this, null) == Sender.Outcome.RETRY;
            jobFinished(params, retry);
        }, "smsledger-send").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;  // stopped by the system (network lost): run again later
    }
}
