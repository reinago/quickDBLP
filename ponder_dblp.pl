# TODO
# if authors re-occur, do we want to add the ORCID?

use IO::Uncompress::Gunzip qw(gunzip $GunzipError);
use strict;
use warnings;

# open dblp.rdf
my $dblp_file = 'dblp.rdf.gz';
# open my $fh, '<:gzip', $dblp_file or die "Could not open file '$dblp_file' $!";
my $fh = IO::Uncompress::Gunzip->new($dblp_file) or die "IO::Uncompress::Gunzip failed: $GunzipError\n";

open my $papers_fh, '>', 'dblp_papers.csv' or die "Could not open file 'dblp_papers.csv' $!";
print $papers_fh "NumericID\tDBLP\tTitle\tYear\n";

open my $authors_fh, '>', 'dblp_authors.csv' or die "Could not open file 'dblp_authors.csv' $!";
print $authors_fh "NumericID\tDBLP\tName\tORCID\n";

open my $papers_authors_fh, '>', 'dblp_papers_authors.csv' or die "Could not open file 'dblp_papers_authors.csv' $!";
print $papers_authors_fh "PaperID\tAuthorID\n";

# remember start time
my $start_time = time();
print "Start time: " . localtime($start_time) . "\n";

# states:
# 0 = searching
# 1 = inproceedings
# 2 = person
# 3 = article
# 4 = informal
# 5 = repository
# 6 = reference

my $state = 0;
my $currPaperID = '';
my $currTitle = '';
my $currName = '';

my $printLevel = 1; # 0 = no print, 1 = print error, 2 = print warning, 3 = print info

# hash for paper IDs
my %papersToNumbers = ();
my $maxPaperNumber = 1;
my $currPapernumber = 0;
# hash for author IDs
my %authorsToNumbers = ();
my $maxAuthorNumber = 1;
my $currAuthorNumber = 0;

my @authorsAndPapers = ();

my @currAuthors = ();
my @currCreators = ();
my $currYear = 0;

sub printInfo {
    my ($msg) = @_;
    if ($printLevel >= 3) {
        print "INFO: $msg\n";
    }
}

sub printWarning {
    my ($msg) = @_;
    if ($printLevel >= 2) {
        print "WARNING: $msg\n";
    }
}

sub printError {
    my ($msg) = @_;
    if ($printLevel >= 1) {
        print "ERROR: $msg\n";
    }
}

sub addPaper {
    my ($line) = @_;
    @currAuthors = ();
    @currCreators = ();

    $line =~ /rdf:about="([^"]+)"/;
    if ($1) {
        $currPaperID = $1;
        printInfo "Current ID: $currPaperID\n";
        $papersToNumbers{$currPaperID} = $maxPaperNumber;
        $currPapernumber = $maxPaperNumber;
        $maxPaperNumber++;
        printInfo "Assigned number $papersToNumbers{$currPaperID} to paper ID $currPaperID\n";
    } else {
        printError "No ID found in entry: $line\n";
    }
}

sub extractResource {
    my ($line) = @_;
    if ($line =~ /rdf:resource="([^"]+)"/) {
        return $1;
    } else {
        printError "No resource found in line: $line\n";
        return undef;
    }
}

sub checkAuthor {
    my ($currAuthorID, $currAuthorOrcid, $currAuthorName) = @_;
    my $realID = 0;
    if (exists $authorsToNumbers{$currAuthorID}) {
        printInfo "Found author ID: $currAuthorID\n";
        $realID = $authorsToNumbers{$currAuthorID};
    } else {
        printInfo "New author ID: $currAuthorID\n";
        $realID = $maxAuthorNumber;
        $authorsToNumbers{$currAuthorID} = $realID;
        printInfo "Assigned number $realID to author ID $currAuthorID\n";
        print $authors_fh "$realID\t$currAuthorID\t$currAuthorName\t$currAuthorOrcid\n";
        $maxAuthorNumber++;
    }
    return $realID;
}

sub processPaperContent {
    my ($line) = @_;
    my $currAuthorID = '';
    my $currAuthorOrcid = '';
    my $currAuthorName = '';

    if ($line =~ /<dblp:title>([^<]+)<\/dblp:title>/) {
        $currTitle = $1;
        printInfo "Current Title: $currTitle\n";
    } elsif (index($line, "<dblp:yearOfPublication") != -1) {
        if ($line =~ /<dblp:yearOfPublication.*?>(\d+)<\/dblp:yearOfPublication>/) {
            $currYear = $1;
            printInfo "Current Year: $currYear\n";
        } else {
            printError "No year found in line: $line\n";
        }
    } elsif ($line =~ /<dblp:authoredBy rdf:resource="([^"]+)"/) {
        printInfo "Found author ID: $1\n";
        push @currAuthors, $1;
    } elsif (index($line, "<dblp:AuthorSignature") != -1) {
        do {
            $line = <$fh>;
            chomp $line;
            if (index($line, "<dblp:signatureCreator") != -1) {
                if(my $res = extractResource($line)) {
                    $currAuthorID = $res;
                } else {
                    printError "No ID found in signatureCreator entry\n";
                }
            } elsif (index($line, "<dblp:signatureOrcid") != -1) {
                if(my $res = extractResource($line)) {
                    $currAuthorOrcid = $res;
                } else {
                    printError "No ID found in signatureOrcid entry\n";
                }
            } elsif (index($line, "<dblp:signatureDblpName") != -1) {
                if ($line =~ /<dblp:signatureDblpName>([^<]+)<\/dblp:signatureDblpName>/) {
                    $currAuthorName = $1;
                }
            }
        } while (index($line, "<\/dblp:AuthorSignature") == -1);
        push @currCreators, [$currAuthorID, $currAuthorOrcid, $currAuthorName];
        printInfo "Found creator: $currAuthorID, $currAuthorOrcid, $currAuthorName\n";
    }
}

sub finalizePaper {
    if ((scalar @currAuthors) != (scalar @currCreators)) {
        printError "Number of authors and creators do not match for paper $currPaperID: " . scalar(@currAuthors) . " vs " . scalar(@currCreators) . "\n";
    } else {
        foreach my $entry (@currCreators) {
            my ($currAuthorID, $currAuthorOrcid, $currAuthorName) = @$entry;
            my $realID = checkAuthor($currAuthorID, $currAuthorOrcid, $currAuthorName);
            print $papers_authors_fh "$currPapernumber\t$realID\n";
        }
    }
    print $papers_fh "$currPapernumber\t$currPaperID\t$currTitle\t$currYear\n";
}

while (my $line = <$fh>) {
    chomp $line;

    # switch depending on state
    if ($state == 0) {
        # Searching for the start of a new entry
        if (index($line, "<dblp:Inproceedings") != -1) {
            $state = 1;
            printInfo "Found Inproceedings entry\n";
            addPaper($line);
        } elsif (index($line, "<dblp:Article") != -1) {
            $state = 3;
            printInfo "Found Article entry\n";
            addPaper($line);
        }
        # elsif (index($line, "<dblp:Person") != -1) {
        #     $state = 2;
        #     printInfo "Found Person entry\n";
        #     $line =~ /rdf:about="([^"]+)"/;
        #     if ($1) {
        #         $currID = $1;
        #         printInfo "Current ID: $currID\n";
        #         $authorsToNumbers{$currID} = $maxAuthorNumber;
        #         $currAuthorNumber = $maxAuthorNumber;
        #         $maxAuthorNumber++;
        #         printInfo "Assigned number $authorsToNumbers{$currID} to author ID $currID\n";
        #     } else {
        #         printError "No ID found in Person entry\n";
        #     }
        # }

        # elsif ($line =~ /<dblp:informal/) {
        #     $state = 4;
        #     print "Found Informal entry\n";
        # } elsif ($line =~ /<dblp:repository/) {
        #     $state = 5;
        #     print "Found Repository entry\n";
        # } elsif ($line =~ /<dblp:reference/) {
        #     $state = 6;
        #     print "Found Reference entry\n";
        # }
    }

    if ($state == 1) {
        if (index($line, "<\/dblp:Inproceedings") != -1) {
            $state = 0;
            printInfo "End of Inproceedings entry\n";
            finalizePaper();
        } else {
            processPaperContent($line);
        }
    }
    if ($state == 3) {
        if (index($line, "<\/dblp:Article") != -1) {
            $state = 0;
            printInfo "End of Article entry\n";
            finalizePaper();
        } else {
            processPaperContent($line);
        }
    }
    # if ($state == 2) {
    #     if (index($line, "<\/dblp:Person") != -1) {
    #         $state = 0;
    #         printInfo "End of Person entry\n";
    #         print $authors_fh "$currAuthorNumber\t$currID\t$currName\n";
    #     } elsif ($line =~ /^<dblp:primaryCreatorName>([^<]+)<\/dblp:primaryCreatorName>/) {
    #         $currName = $1;
    #         printInfo "Current Author Name: $currName\n";
    #     }
    # }
}

# now clean up the author-paper relations
# foreach my $entry (@authorsAndPapers) {
#     my ($paperID, $authorID) = @$entry;
#     if (exists $authorsToNumbers{$authorID}) {
#         print $papers_authors_fh "$paperID\t$authorsToNumbers{$authorID}\n";
#     } else {
#         printError "Author ID not found in hash: $authorID\n";
#     }
# }

close $fh;
close $papers_fh;
close $authors_fh;
close $papers_authors_fh;

# get end time
my $end_time = time();
print "End time: " . localtime($end_time) . "\n";

# print elapsed time
my $elapsed_time = $end_time - $start_time;
my $hours = int($elapsed_time / 3600);
my $minutes = int(($elapsed_time % 3600) / 60);
my $seconds = $elapsed_time % 60;
print "Elapsed time: $hours hours, $minutes minutes, $seconds seconds\n";
