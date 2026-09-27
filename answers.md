# Easy Apply answers

The agent answers Easy Apply questions ONLY from this file and `cv.yaml`.
If a *required* question is not covered here, it skips the job and logs the question
(`applied.jsonl`, status `skipped`), so you can add the answer and it gets covered next run.

How to fill:
- Fill in every line still marked as to do. Delete lines that don't apply to you instead.
- Be literal. The agent types these values, so "3" is better than "about three".
- Pre-filled values come from cv.yaml; correct anything that is wrong.

## Agent policy
- Numeric fields: integers only, no units ("3", not "3 years").
- Yes/No questions about a skill with no line below: answer "No". Never claim a skill not in cv.yaml.
- Optional free-text fields (cover letter, "anything else"): leave empty.
- Optional questions not covered here: leave them blank or pick "Prefer not to say".
- Voluntary self-identification (gender, ethnicity, veteran, disability): answer exactly what is under "Self-identification".

## Contact (LinkedIn usually pre-fills these from your profile)
- Email: dgarciacainzos@gmail.com
- Phone country code: Spain (+34)
- Mobile phone: 664410502
- City: Valencia, Spain
- LinkedIn: https://www.linkedin.com/in/diegocainzos
- GitHub: https://github.com/diegocainzos
- Portfolio / website: https://diegocainzos.cv

## Work authorization & location
- Citizenship: EU citizen (TODO: country of citizenship)
- Authorized to work in the EU / EEA without sponsorship: Yes
- Authorized to work in Switzerland: TODO
- Authorized to work in the UK: TODO
- Authorized to work in the US: TODO
- Require visa sponsorship now or in the future for EU jobs: No
- Require visa sponsorship for UK / US jobs: TODO
- Willing to relocate: Yes (Berlin / EU) (confirmed)
- Countries or cities you will NOT relocate to: TODO
- Comfortable working on-site / hybrid / remote: TODO (e.g. "all three")
- Max on-site days per week you accept: TODO
- Willing to commute (how far): TODO
- Willing to travel for work (% of time): TODO

## Availability
- Notice period: TODO (e.g. "0", "2 weeks", "1 month")
- Earliest start date: TODO (YYYY-MM-DD or "immediately")
- Currently employed: Yes (AI Engineer at Navertia, Valencia, since April 2026, hybrid)
- Open to: full-time, TODO (part-time? contract? internship?)
- Willing to work shifts / weekends / on-call: TODO

## Compensation
- Expected gross annual salary, EUR: TODO (single number, e.g. 38000)
- Expected gross annual salary, GBP: TODO
- Expected gross annual salary, USD: TODO
- Expected hourly / daily rate for contracts, EUR: TODO
- Current salary: TODO (or "Prefer not to say")
- Is the salary negotiable: TODO

## Experience (years, integers)
- Total professional experience: 2
- Software development: 2
- Python: 2
- Machine learning / AI: 2
- LLMs / generative AI: 2
- LangChain / LangGraph: 2
- PyTorch: 2
- Django: 0 (not in cv.yaml)
- SQL: 2
- Docker: 2
- Git: 2
- Linux / Bash: 2
- System administration / virtualization: 2
- Banking / fintech industry: 0
- Team lead / management: 0
- Add more lines for any tool that shows up often in questions: TODO

## Education
- Highest completed degree: Bachelor's (BS Computer Science — Data and Software, Universidade da Coruña, 2020-2024)
- In progress: MS in Natural Language Processing, UNED (online, since 2025-10); coursework completed, thesis pending
- Field of study: Computer Science (Data and Software)
- GPA / final grade: TODO (or delete)
- Completed a Bachelor's degree: Yes
- Completed a Master's degree: No (coursework done, thesis pending)

## Languages (CEFR)
- Spanish: Native
- English: C1 (IELTS 7.5), professional working proficiency
- Portuguese: B2
- Italian: A2
- German: TODO ("None" is a valid answer)

## Certifications & licenses
- Certifications: IELTS 7.5 (TODO: add others or delete)
- Driver's license: TODO (e.g. "Yes, EU category B")
- Own a car: TODO
- Security clearance: No

## Legal & screening
- 18 years or older: Yes
- Consent to background check: TODO
- Consent to drug test: TODO
- Previously worked for this company: No (agent: check cv.yaml experience first)
- Referred by a current employee: No
- Related to anyone at the company: No
- Bound by a non-compete: TODO

## Self-identification (voluntary; mostly US forms)
- Gender: Prefer not to say
- Pronouns: Prefer not to say
- Race / ethnicity: Prefer not to say
- Veteran status: I am not a protected veteran
- Disability: Prefer not to say

## Short free-text answers (used only when the field is required)
- Why are you interested in this role: TODO (1-2 generic sentences; the agent will adapt the company name)
- Describe yourself / summary: use the summary from cv.yaml
- How did you hear about us: LinkedIn
