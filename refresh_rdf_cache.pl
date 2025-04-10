use LWP::Simple;
use DateTime;

my $dblp_file = 'dblp.rdf.gz';

my $max_age_days = 14; # max age of the file in days
my $download_from_dblp = 0;
my $dblp_dir = 'https://dblp.org/rdf/';
# check if the file exists
if (!-e $dblp_file) {
    print "File $dblp_file not found.\n";
    $download_from_dblp = 1;
} else {
    # read modification DateTime of dblp.rdf.gz file
    my $filetime = DateTime->from_epoch(epoch => (stat($dblp_file))[9], time_zone => 'local');

    # get the DBLP directory
    $contents = get($dblp_dir) or die "Couldn't get the rdf directory!";
    for $line (split /\n/, $contents) {
        if ($line =~ />$dblp_file</) {
            # print $line;
            if ($line =~ />(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s*</) {
                # print "found year $1 month $2 day $3\n";
                $onlinetime = DateTime->new(
                    year => $1,
                    month => $2,
                    day => $3,
                    hour => $4,
                    minute => $5,
                    second => 0,
                    time_zone => 'local',
                );
                $diff = $onlinetime->subtract_datetime($filetime)->in_units('days');
                if ($diff > $max_age_days) {
                    print "File $dblp_file is too old ($diff days).\n";
                    $download_from_dblp = 1;
                }
            }
        }
    }
}

if ($download_from_dblp) {
    print "Fetching remote file, please wait... ";
    flush STDOUT;
    $data = get("$dblp_dir$dblp_file") or die "Couldn't get $dblp_file!";
    open(my $fh, '>:raw', $dblp_file) or die "Couldn't open file for writing!";
    print $fh $data;
    close($fh);
    print "done.\n";
} else {
    print "File $dblp_file is up to date (less than $max_age_days days old).\n";
}