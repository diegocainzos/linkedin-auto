# Easy Apply answers

> Copy to answers.md (gitignored) and replace every value with your own.

The agent answers Easy Apply questions from this file and `cv.yaml`.
A question not covered here doesn't stop an application: the agent infers the answer from `cv.yaml`, or
answers Yes / the most favorable option, and adds the question with its answer at the end of this file
("Answered by the agent"). Later jobs reuse that answer. Review that section and correct what's wrong.

How to fill:
- Fill in every line still marked as to do. Delete lines that don't apply to you instead.
- Be literal. The agent types these values, so "3" is better than "about three".
- Pre-filled values come from cv.yaml; correct anything that is wrong.

## Agent policy
- Numeric fields: integers only, no units ("3", not "3 years").
- Yes/No questions with no line below: answer "Yes".
- Optional free-text fields (cover letter, "anything else"): leave empty.
- Optional questions not covered here: leave them blank or pick "Prefer not to say".
- Voluntary self-identification (gender, ethnicity, veteran, disability): answer exactly what is under "Self-identification".

## Contact (LinkedIn usually pre-fills these from your profile)
- Email: jane.doe@example.com
- Phone country code: Spain (+34)
- Mobile phone: 600000000
- City: Valencia, Spain
- LinkedIn: https://www.linkedin.com/in/janedoe
- GitHub: https://github.com/janedoe
- Portfolio / website: https://example.com

## Work authorization & location
- Citizenship: EU citizen (Spain)
- Authorized to work in the EU / EEA without sponsorship: Yes
- Authorized to work in Switzerland: Yes (EU/EFTA free movement)
- Authorized to work in the UK: No
- Authorized to work in the US: No
- Require visa sponsorship now or in the future for EU jobs: No
- Require visa sponsorship for UK / US jobs: Yes
- Willing to relocate: Yes
- Countries or cities you will NOT relocate to: None
- Comfortable working on-site / hybrid / remote: All three
- Max on-site days per week you accept: 5
- Willing to commute (how far): Up to 1 hour each way
- Willing to travel for work (% of time): 25

## Availability
- Notice period: 2 weeks
- Earliest start date: 2 weeks after accepting an offer
- Currently employed: Yes (AI Engineer at Example Corp, since January 2025, hybrid)
- Open to: full-time, contract
- Willing to work shifts / weekends / on-call: No

## Compensation
- Expected gross annual salary, EUR: 40000
- Expected gross annual salary, GBP: 35000
- Expected gross annual salary, USD: 50000
- Expected hourly / daily rate for contracts, EUR: 30 / 240
- Current salary: Prefer not to say
- Is the salary negotiable: Yes

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

## Education
- Highest completed degree: Bachelor's (BS Computer Science, Example University, 2020-2024)
- In progress: None
- Field of study: Computer Science
- Completed a Bachelor's degree: Yes
- Completed a Master's degree: No

## Languages (CEFR)
- Spanish: Native
- English: C1
- German: None

## Certifications & licenses
- Certifications: None
- Driver's license: No
- Own a car: No
- Security clearance: No

## Legal & screening
- 18 years or older: Yes
- Consent to background check: Yes
- Consent to drug test: Yes
- Previously worked for this company: No (agent: check cv.yaml experience first)
- Referred by a current employee: No
- Related to anyone at the company: No
- Bound by a non-compete: No

## Self-identification (voluntary; mostly US forms)
- Gender: Prefer not to say
- Pronouns: Prefer not to say
- Race / ethnicity: Prefer not to say
- Veteran status: I am not a protected veteran
- Disability: Prefer not to say

## Short free-text answers (used only when the field is required)
- Why are you interested in this role: The role matches my experience building LLM and ML systems in Python, and I want to keep growing as an AI engineer on a team shipping real products.
- Describe yourself / summary: use the summary from cv.yaml
- How did you hear about us: LinkedIn
