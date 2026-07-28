"""
Convert Semicons Labs platform data into indexable text chunks for RAG.

Processes:
1. API dump (certifications, modules, skills, domains, tools)
2. Platform features (workflows, user roles, subscriptions)

Output: Text chunks ready for embedding and vector search.
"""

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)

API_DUMP_PATH = os.path.join(_PARENT, "semicon_api_dump.json")


def load_api_dump():
    """Load the API dump JSON."""
    with open(API_DUMP_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def format_certification(cert):
    """Convert certification data to searchable text."""
    tool_name = cert.get("tool", {}).get("name", "N/A") if isinstance(cert.get("tool"), dict) else "N/A"

    text = f"""Certification: {cert['title']}
Domain: {cert['domain_name']}
Tool: {tool_name}
Level: {cert['level_of_difficulty']}
Duration: {cert.get('duration', 'N/A')} hours
Description: {cert['description']}
Modules: {cert['modules_count']} modules
Practicals: {cert['practicals']} lab practicals
Lab Testcases: {cert['lab_testcases_count']} testcases

This certification covers the complete flow for {cert['domain_name']} using {tool_name} tools.
"""
    return {
        "content": text.strip(),
        "metadata": {
            "source": "certification",
            "title": cert["title"],
            "domain": cert["domain_name"],
            "tool": tool_name,
            "level": cert["level_of_difficulty"]
        }
    }


def format_module(module):
    """Convert module data to searchable text."""
    text = f"""Module: {module['name']}
External ID: {module.get('external_module_id', 'N/A')}
Level: {module['level']}
Duration: {module['duration']} hours
Description: {module['description']}
Lab Testcases: {module['lab_testcases_count']} testcases
Practicals: {module['practicals']} practicals

This module teaches you how to {module['description'].lower()}
"""
    return {
        "content": text.strip(),
        "metadata": {
            "source": "module",
            "name": module["name"],
            "level": module["level"],
            "external_id": module.get("external_module_id", "")
        }
    }


def format_skill(skill):
    """Convert skill data to searchable text."""
    tool_name = skill.get("tool", {}).get("name", "N/A") if isinstance(skill.get("tool"), dict) else "N/A"
    domain_name = skill.get("domain", {}).get("name", "N/A") if isinstance(skill.get("domain"), dict) else "N/A"

    text = f"""Skill: {skill['title']}
Domain: {domain_name}
Tool: {tool_name}
Level: {skill['level_of_difficulty']}
Duration: {skill.get('duration', 'N/A')} hours
Description: {skill['description']}
Modules: {skill['modules_count']} modules
Lab Testcases: {skill['lab_testcases_count']} testcases
Practicals: {skill['practicals']} practicals

This skill provides hands-on experience with {tool_name} for {domain_name}.
"""
    return {
        "content": text.strip(),
        "metadata": {
            "source": "skill",
            "title": skill["title"],
            "domain": domain_name,
            "tool": tool_name
        }
    }


def format_domain(domain):
    """Convert domain data to searchable text."""
    text = f"""Domain: {domain['name']}
Description: {domain.get('description', 'Core VLSI domain')}

Semicons Labs offers comprehensive training in {domain['name']}, covering industry-standard tools and methodologies.
"""
    return {
        "content": text.strip(),
        "metadata": {
            "source": "domain",
            "name": domain["name"]
        }
    }


def format_tool(tool):
    """Convert tool data to searchable text."""
    text = f"""Tool Vendor: {tool['name']}

Semicons Labs provides hands-on training with {tool['name']} tools for various VLSI domains including Physical Design, Design Verification, and Analog Layout.
"""
    return {
        "content": text.strip(),
        "metadata": {
            "source": "tool",
            "name": tool["name"]
        }
    }


def create_platform_features():
    """Create text chunks for platform features and workflows."""
    features = []

    # Subscription Plans
    features.append({
        "content": """Subscription Plans: Basic vs Pro

Semicons Labs offers two subscription tiers:

Basic Plan:
- Access to one tool vendor (Synopsys OR Cadence OR Siemens)
- Cannot switch between tools
- Standard lab access and hours
- All course content included

Pro Plan:
- Tool switching enabled - freely switch between Synopsys, Cadence, and Siemens
- Access to all vendor tools for the same domain
- Same lab access as Basic
- Recommended for comprehensive learning

Users with Basic plan will see "Upgrade to Pro" option when attempting to switch tools.
""",
        "metadata": {"source": "platform_feature", "category": "subscription"}
    })

    # Lab Launch Workflow
    features.append({
        "content": """Lab Launch Process

How to start a lab in Semicons Labs:

1. Preview the Skill - Browse available skills and read descriptions
2. Enroll in Skill - Click enroll to add the skill to your learning path
3. Navigate to Competency - Open the specific module/competency
4. Read Lab Instructions - Review lab setup guide and prerequisites
5. Click "Start Lab" - Launch the lab environment
6. Lab Opens - Begin working on the practical exercise

Your lab hours are automatically tracked in your profile. You can see session details, time used, and remaining hours.
""",
        "metadata": {"source": "platform_feature", "category": "lab_workflow"}
    })

    # Progress Tracking
    features.append({
        "content": """Progress Tracking

Semicons Labs provides multiple ways to track your learning progress:

Journey Tab:
- Visual representation of your learning path
- Shows completed modules, skills, and competencies
- Filter by tool (Synopsys/Cadence/Siemens)
- Progress legends and completion percentage

Dashboard Analytics:
- Updates when you enroll or complete skills
- Shows active skills and completion status
- Lab hours used vs allocated

Profile:
- Lab hours tracking with validity period
- Session details and subscription information
- Number of sessions allocated
""",
        "metadata": {"source": "platform_feature", "category": "progress"}
    })

    # Certificates
    features.append({
        "content": """Certificates

After completing a skill (all competencies and quizzes), learners receive a certificate:

- Downloadable from the platform
- Available after skill completion
- Includes skill name, completion date, and learner details
- Certificates are issued per skill

Completion Requirements:
- Complete all competencies/modules in the skill
- Pass all quizzes (given after each competency/module)
- Complete all required lab practicals
""",
        "metadata": {"source": "platform_feature", "category": "certificates"}
    })

    # Quiz System
    features.append({
        "content": """Quiz System

Semicons Labs includes quizzes to test your understanding:

- Quiz appears after completion of each competency/module
- Tests knowledge of concepts covered in that module
- Must pass quizzes to complete the skill
- Quizzes are part of the certification requirement

The quiz system ensures learners have grasped the fundamental concepts before moving forward.
""",
        "metadata": {"source": "platform_feature", "category": "assessment"}
    })

    # Support & Tickets
    features.append({
        "content": """Support and Ticket System

If you encounter issues, Semicons Labs provides a support ticket system:

Who Can Raise Tickets:
- Individual Learners
- Teams Learners
- Client Admins
- Managers

How to Raise a Ticket:
1. Navigate to Support section
2. Click "Create Ticket"
3. Describe the issue
4. Submit ticket

Tracking:
- View all your tickets
- Check ticket status (open, in progress, resolved)
- Receive updates on resolution

Support handles lab access issues, platform bugs, and technical questions.
""",
        "metadata": {"source": "platform_feature", "category": "support"}
    })

    # Knowledge Base
    features.append({
        "content": """Knowledge Base

Semicons Labs provides a Knowledge Base for reference and help:

- Accessible while working in labs
- Contains document content for all courses
- Reference materials for tools and concepts
- Lab guides and troubleshooting tips
- Available in-platform for quick access

Use the Knowledge Base when you need help or want to reference documentation while working on labs.
""",
        "metadata": {"source": "platform_feature", "category": "documentation"}
    })

    # User Types
    features.append({
        "content": """User Types and Roles

Semicons Labs supports three user categories:

1. Individual Learners:
   - Self-enrollment through website
   - Purchase own subscription
   - Full access to courses and labs
   - Independent learning

2. Teams:
   - Teams POC: Explores plans, makes purchase
   - Teams Admin: Manages all users, assigns licenses
   - Teams Manager: Manages allocated learners, sends notifications
   - Teams Learners: Access courses as assigned by admin/manager

3. Corporates:
   - Corporate POC: Submits inquiry/request
   - Corporate Admin: Manages enterprise users and licenses
   - Corporate Manager: Handles sub-teams and user allocation
   - Corporate Learners: Enterprise learners with company-provided access

Each role has specific permissions and capabilities within the platform.
""",
        "metadata": {"source": "platform_feature", "category": "user_roles"}
    })

    # Admin User Management
    features.append({
        "content": """Admin and Manager User Management

Teams and Corporate Admins can manage learners:

Adding Users Manually:
- Add learners one by one
- Specify Basic or Pro plan
- Assign domain access
- Set lab hours/sessions allocation

Bulk Upload:
- Upload CSV file with user details
- CSV must include: name, email, subscription type (Basic/Pro), sessions allocated
- Empty or incorrect details will reject the upload
- All user metadata must be complete

Manager Capabilities:
- Managers can only add users (cannot create other managers)
- Can activate/deactivate users they added
- Track active/inactive users and lab activity
- Send in-app notifications to their learners

Admin Capabilities:
- Add both learners and managers
- Assign user allocations to managers
- Full user management control
- Track all user activity across the organization
""",
        "metadata": {"source": "platform_feature", "category": "admin"}
    })

    # Email Notifications
    features.append({
        "content": """Email Notifications

Semicons Labs sends various emails to users:

Individual Learners:
- Welcome email after payment with subscription details
- Receipt/invoice with payment information
- Sign-in credentials

Teams/Corporate Learners:
- Account creation email with sign-up link
- Welcome message with manager/admin name
- Subscription details (plan, hours, validity)

Admins:
- Confirmation email with purchased licenses
- Request to provide admin details
- Plan summary with pro/basic user counts

Managers:
- Email when assigned by admin
- List of allocated learners
- Sign-up link to create account

Renewal Reminders:
- Teams admin receives renewal emails 7 days before subscription expiry

All emails include relevant subscription details and next steps.
""",
        "metadata": {"source": "platform_feature", "category": "notifications"}
    })

    # Enrollment Flow
    features.append({
        "content": """Course Enrollment Flow

Individual Learners - Full Flow:
1. Explore website and browse pricing/plans
2. Select domain and subscription plan
3. Complete registration with details
4. Make payment
5. Receive welcome email with confirmation
6. Sign in from email link
7. Take platform tour (first-time login)
8. Explore and update profile
9. Preview skills before enrolling
10. Enroll in desired skills
11. Start learning

Teams/Corporate Learners - Flow:
1. Admin/Manager adds learner to platform
2. Learner receives email with account details
3. Sign up using link in email
4. Explore platform and update profile
5. Preview and enroll in skills (based on assigned access)
6. Start learning

Skills show as "enrolled" in dashboard analytics once enrolled.
""",
        "metadata": {"source": "platform_feature", "category": "enrollment"}
    })

    return features


def generate_all_chunks():
    """Generate all text chunks from API dump and platform features."""
    api_data = load_api_dump()
    chunks = []

    # Process certifications (from landing page)
    if "landing" in api_data and "body" in api_data["landing"]:
        certs = api_data["landing"]["body"].get("data", {}).get("certifications", [])
        for cert in certs:
            chunks.append(format_certification(cert))

    # Process modules
    if "modules" in api_data and "body" in api_data["modules"]:
        modules = api_data["modules"]["body"].get("data", [])
        for module in modules:
            chunks.append(format_module(module))

    # Process skills
    if "skills" in api_data and "body" in api_data["skills"]:
        skills = api_data["skills"]["body"].get("data", [])
        for skill in skills:
            chunks.append(format_skill(skill))

    # Process domains
    if "domains" in api_data and "body" in api_data["domains"]:
        domains = api_data["domains"]["body"]
        for domain in domains:
            chunks.append(format_domain(domain))

    # Process tools
    if "tools" in api_data and "body" in api_data["tools"]:
        tools = api_data["tools"]["body"]
        for tool in tools:
            chunks.append(format_tool(tool))

    # Add platform features
    chunks.extend(create_platform_features())

    return chunks


if __name__ == "__main__":
    chunks = generate_all_chunks()
    print(f"Generated {len(chunks)} text chunks from platform data")

    # Show a sample
    print("\n=== SAMPLE CHUNK ===")
    print(chunks[0]["content"])
    print(f"\nMetadata: {chunks[0]['metadata']}")
