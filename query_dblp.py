import pandas as pd
from datetime import datetime
from itables import init_notebook_mode, show
import ipywidgets as widgets
from IPython.display import display
from lxml import html
import duckdb

con = None
init_notebook_mode(connected=True)


def prepare_data(snapshot_dir):
    global con
    con = duckdb.connect(database=':memory:', read_only=False)
    con.execute(f"create table authors as select * from read_csv('{snapshot_dir}/dblp_authors.csv')")
    con.execute(f"create table papers as select * from read_csv('{snapshot_dir}/dblp_papers.csv')")
    con.execute(f"create table papers_authors as select * from read_csv('{snapshot_dir}/dblp_papers_authors.csv')")

    con.execute("prepare find_author as select * from authors where starts_with(upper(Name), upper($name))")
    con.execute("prepare find_author_like as select * from authors where Name ilike $name")
    con.execute("prepare find_author_dblp as select * from authors where DBLP = $dblp")
    con.execute("prepare find_coauthors as select * from papers_authors join papers on papers_authors.PaperID = papers.NumericID join authors on papers_authors.AuthorID = authors.NumericID where AuthorID in $list and Year >= $year")
    con.execute("prepare find_copapers as select * from papers_authors join papers on papers_authors.PaperID = papers.NumericID join authors on papers_authors.AuthorID = authors.NumericID where PaperID in (select PaperID from papers_authors where AuthorID in $list) and Year >= $year")


def show_search_UI():
    search = ""
    result = ""
    text_blob = ""
    cutoff = 5
    ignore_cutoff = False
    explain = False
    description_width = '200px'

    # Create UI elements
    search_input = widgets.Text(value=search, description='Search (semicolon separated):')
    search_input.layout.width = '600px'
    search_input.style.description_width = description_width
    matcher = widgets.Dropdown(options=['startswith', 'like'], value='startswith', description='Matcher:')
    matcher.layout.width = '400px'
    matcher.style.description_width = description_width
    search_prefilter = widgets.Text(value="", description='Prefilter result DBLP IDs:')
    search_prefilter.layout.width = '600px'
    search_prefilter.style.description_width = description_width
    cutoff_slider = widgets.IntSlider(value=cutoff, min=1, max=10, step=1, description='Cutoff (years):')
    cutoff_slider.layout.width = '400px'
    cutoff_slider.style.description_width = description_width
    cutoff_toggle = widgets.Checkbox(value=ignore_cutoff, description='Ignore cutoff')
    cutoff_group = widgets.HBox([cutoff_slider, cutoff_toggle])
    explain_toggle = widgets.Checkbox(value=explain, description='Explain')
    explain_toggle.style.description_width = description_width
    search_button = widgets.Button(description="Find Co-Authors")
    search_button.layout.margin = '0px 0px 0px 210px'
    search_output = widgets.HTML(value=result, description='Hits:')
    search_output.layout.width = '1400px'
    search_output.style.description_width = description_width
    # search_output.layout.height = '150px'
    output = widgets.Output()

    page_text = widgets.Textarea(value=text_blob, description='Webpage Source:')
    page_text.layout.width = '600px'
    page_text.style.description_width = description_width
    page_text.layout.height = '150px'
    guesser = widgets.Dropdown(options=['PCS', 'EasyChair'], value='PCS', description='Guesser:')
    guesser.layout.width = '400px'
    guesser.style.description_width = description_width
    guess_button = widgets.Button(description="Guess Authors")
    guess_button.layout.margin = '0px 0px 0px 210px'

    # Display UI elements
    display(page_text, guesser, guess_button, search_input, matcher, search_prefilter, cutoff_group, explain_toggle, search_button, search_output, output)

    def on_guess_button_clicked(b):
        with output:
            output.clear_output()
            # text = "\n".join(page_text.value.splitlines())
            text = page_text.value
            guesser_value = guesser.value
            if guesser_value == 'PCS':
                tree = html.fromstring(text)

                # Search for elements with the class "authorList"
                author_list_elements = tree.find_class("authorList")[0]
                # Extract <li> elements from the "authorList"
                li_elements = author_list_elements.xpath(".//li")

                # Extract the first <span> from each <li>
                guessed_authors = []
                for li in li_elements:
                    span = li.find(".//span")  # Find the first <span> in the <li>
                    if span is not None:
                        guessed_authors.append(span.text_content())
                search_input.value = "; ".join(guessed_authors)
            elif guesser_value == 'EasyChair':
                tree = html.fromstring(text)
                tables = tree.find_class("ct_table")
                for table in tables:
                    if table.find(".//tr").find(".//td").text_content() == "Authors":
                        rows = table.xpath(".//tr")
                        guessed_authors = []
                        for row in rows[2:]:
                            data = row.xpath(".//td")
                            if len(data) > 1:
                                author = data[0].text_content() + " " + data[1].text_content()
                                if author:
                                    guessed_authors.append(author)
                search_input.value = "; ".join(guessed_authors)

    def on_search_button_clicked(b):
        # Update variables based on UI input
        search = search_input.value
        cutoff = cutoff_slider.value
        explain = explain_toggle.value
        ignore_cutoff = cutoff_toggle.value

        with output:
            output.clear_output()
            if ignore_cutoff:
                cutoffYear = 0
            else:
                cutoffYear = datetime.now().year - cutoff
            hits_list = []
            # hits_frame = pd.DataFrame(data=None, columns=authors.columns, index=authors.index)
            hits_frame = pd.DataFrame(data=None)
            for s in search.split(";"):
                # h = authors[authors["Name"].str.contains(s.strip(), case=False)]
                cleaned = s.strip()
                if cleaned.startswith("https://dblp.org"):
                    h = con.execute(f"execute find_author_dblp(dblp:='{cleaned}')").df()
                else:
                    if cleaned.find(",") > -1:
                        cleaned = cleaned.split(",")
                        cleaned = cleaned[1].strip() + " " + cleaned[0].strip()
                    if matcher.value == "startswith":
                        h = con.execute(f"execute find_author(name:='{cleaned}')").df()
                    elif matcher.value == "like":
                        h = con.execute(f"execute find_author_like(name:='{cleaned}')").df()
                hits_list.append(h)
            hits_frame = pd.concat(hits_list, ignore_index=True)
            if hits_frame.empty:
                # print(f"No authors found for {search}")
                search_output.value = f"No authors found for {search}"
            else:
                authors_ids = []
                hits_frame["DBLP"] = ['<a href="{}">{}</a>'.format(d, d) for d in hits_frame["DBLP"]]
                hits_frame["ORCID"] = ['<a href="{}">{}</a>'.format(d, d) for d in hits_frame["ORCID"]]
                search_output.value = hits_frame.to_html(escape=False, index=False, justify="left")
                for h in hits_frame.itertuples():
                    # print(f"Author: {h.Name}, NumericID: {h.NumericID}, ORCID: {h.ORCID}, DBLP: {h.DBLP}")
                    # search_output.value = f"No authors found for {search}"
                    id = h.NumericID
                    authors_ids.append(id)
                    # Get all papers for these authors
                everything = con.execute(f"execute find_copapers(list:={authors_ids}, year:={cutoffYear})").df()

                if search_prefilter.value:
                    pref = search_prefilter.value.strip().split(";")
                    pref = [p.strip() for p in pref]
                    everything = everything[everything["DBLP_1"].isin(pref)]

                if explain:
                    everything = everything.sort_values(by=["PaperID"])
                    everything = everything[["DBLP", "Title", "Name", "DBLP_1", "ORCID", "Year"]]
                    everything["DBLP"] = ['<a href="{}">{}</a>'.format(d, d) for d in everything["DBLP"]]
                    everything["DBLP_1"] = ['<a href="{}">{}</a>'.format(d, d) for d in everything["DBLP_1"]]
                    everything = everything.rename(columns={"DBLP": "DBLP Paper", "DBLP_1": "DBLP Author"})
                else:
                    everything = everything.sort_values(by=["Name"]).groupby("AuthorID").agg('first')
                    everything = everything[["Name", "DBLP_1", "ORCID", "Year"]]
                    everything["DBLP_1"] = ['<a href="{}">{}</a>'.format(d, d) for d in everything["DBLP_1"]]
                    everything = everything.rename(columns={"DBLP_1": "DBLP Author"})
                # TODO show as datawrangler table mimetype instead
                display(everything)

    def params_changed(change):
        # print(f"Value changed: {change['new']}")
        on_search_button_clicked(None)

    cutoff_slider.observe(params_changed, names='value')
    cutoff_toggle.observe(params_changed, names='value')
    explain_toggle.observe(params_changed, names='value')
    search_button.on_click(on_search_button_clicked)
    guess_button.on_click(on_guess_button_clicked)
