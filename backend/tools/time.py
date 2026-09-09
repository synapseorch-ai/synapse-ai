from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ToolAnnotations, Tool, TextContent
from datetime import datetime, timedelta
import calendar
import zoneinfo
import json

# Initialize MCP server
server = Server("time-agent")

@server.list_tools()
async def list_tools() -> list[Tool]:
    """List available time tools."""
    return [
        Tool(
            name="get_datetime",
            annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=False),
            description="""Get the current or future date as a 10-digit Unix timestamp based on natural language inputs.

PARAMETER COMBINATIONS & LOGIC:

1. WEEKS + WEEKDAY:
   - Apply weeks offset FIRST, then find the weekday from that week
   - Example: {weeks: 1, weekday: "friday"} = next week's Friday
   
2. MONTHS + DATE (numeric):
   - Complete month offset FIRST, then take the date from that month
   - Example: {months: 1, date: "16"} = 16th of next month
   
3. MONTH (name) + DATE:
   - Go to that month and date in the CURRENT year
   - If already passed this year, use NEXT year
   - Example: {month: "april", date: 15} = April 15 of current/next year
   
4. MONTH + DATE + YEAR:
   - Find that exact month and date in the specified year
   - Example: {month: "april", date: 15, year: 2027} = April 15, 2027

EXAMPLES:

"next friday":
  {weeks: 1, weekday: "friday"}
  → Advance 1 week, then find Friday in that week

"16th next month":
  {months: 1, date: "16"}
  → Advance 1 month, then go to the 16th

"April 15":
  {month: "april", date: 15}
  → April 15 of current year (or next year if already passed)

"April 15, 2027":
  {month: "april", date: 15, year: 2027}
  → April 15, 2027

"tomorrow":
  {days: 1}
  → Add 1 day to current date

"in 2 weeks on Monday":
  {weeks: 2, weekday: "monday"}
  → Advance 2 weeks, then find Monday""",
            inputSchema={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "Timezone name (e.g., 'UTC', 'America/New_York', 'Asia/Kolkata'). Default: UTC",
                        "default": "UTC"
                    },
                    "days": {
                        "type": "integer",
                        "description": "Add N days to current date"
                    },
                    "weeks": {
                        "type": "integer",
                        "description": "Add N weeks. If combined with weekday, weeks offset is applied FIRST"
                    },
                    "months": {
                        "type": "integer",
                        "description": "Add N months. If combined with date (numeric), month offset is applied FIRST"
                    },
                    "weekday": {
                        "type": "string",
                        "description": "Target weekday (monday, tuesday, wednesday, thursday, friday, saturday, sunday). If weeks is provided, find this weekday in the target week",
                        "enum": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                    },
                    "date": {
                        "type": "string",
                        "description": "Day of month (1-31). Used with months parameter OR as part of dd/mm format"
                    },
                    "month": {
                        "type": "string",
                        "description": "Month name (january, february, ..., december). Goes to this month in current year (or next year if passed)",
                        "enum": ["january", "february", "march", "april", "may", "june", 
                                "july", "august", "september", "october", "november", "december"]
                    },
                    "year": {
                        "type": "integer",
                        "description": "Specific year (e.g., 2027). Used with month and date parameters"
                    }
                }
            }
        )
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    if name == "get_datetime":
        try:
            # Extract parameters
            timezone = arguments.get("timezone", "UTC")
            days = arguments.get("days")
            weeks = arguments.get("weeks")
            months = arguments.get("months")
            weekday = arguments.get("weekday")
            date_param = arguments.get("date")
            month_param = arguments.get("month")
            year_param = arguments.get("year")
            
            # Initialize timezone and base date
            tz = zoneinfo.ZoneInfo(timezone)
            base_date = datetime.now(tz)
            target_date = base_date
            
            # LOGIC PRIORITY:
            # 1. Handle days offset
            if days is not None:
                target_date += timedelta(days=days)
            
            # 2. Handle WEEKS + WEEKDAY combination (weeks first, then weekday)
            if weeks is not None:
                target_date += timedelta(weeks=weeks)
            
            # 3. Handle MONTHS + DATE combination (months first, then date)
            if months is not None:
                year = target_date.year
                month = target_date.month + months
                
                # Normalize month and year
                while month > 12:
                    month -= 12
                    year += 1
                while month < 1:
                    month += 12
                    year -= 1
                
                # Handle day overflow (e.g., Jan 30 + 1 month -> Feb 30 invalid)
                last_day = calendar.monthrange(year, month)[1]
                day = min(target_date.day, last_day)
                target_date = target_date.replace(year=year, month=month, day=day)
                
                # If date parameter exists with months, set to that specific date
                if date_param is not None and '/' not in str(date_param):
                    try:
                        day = int(date_param)
                        last_day = calendar.monthrange(year, month)[1]
                        if 1 <= day <= last_day:
                            target_date = target_date.replace(day=day)
                    except (ValueError, TypeError):
                        pass
            
            # 4. Handle MONTH (name) + DATE + optional YEAR
            if month_param is not None:
                month_names = ['january', 'february', 'march', 'april', 'may', 'june',
                              'july', 'august', 'september', 'october', 'november', 'december']
                month_lower = month_param.lower().strip()
                
                if month_lower in month_names:
                    target_month = month_names.index(month_lower) + 1
                    target_year = year_param if year_param is not None else base_date.year
                    
                    # Get day from date parameter or keep current day
                    if date_param is not None and '/' not in str(date_param):
                        try:
                            target_day = int(date_param)
                        except (ValueError, TypeError):
                            target_day = target_date.day
                    else:
                        target_day = target_date.day
                    
                    # Validate and clamp day
                    last_day = calendar.monthrange(target_year, target_month)[1]
                    target_day = min(target_day, last_day)
                    
                    # Create candidate date
                    candidate = base_date.replace(year=target_year, month=target_month, day=target_day)
                    
                    # If no year specified and date has passed, use next year
                    if year_param is None and candidate.date() < base_date.date():
                        candidate = candidate.replace(year=target_year + 1)
                    
                    target_date = candidate
            
            # 5. Handle WEEKDAY (after weeks offset if applicable)
            if weekday is not None:
                weekday_lower = weekday.lower().strip()
                weekdays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                
                if weekday_lower not in weekdays:
                    return [TextContent(
                        type="text",
                        text=json.dumps({
                            "error": f"Invalid weekday '{weekday}'. Must be one of {weekdays}"
                        })
                    )]
                
                current_weekday = target_date.weekday()  # Mon=0, Sun=6
                target_weekday = weekdays.index(weekday_lower)
                
                days_ahead = target_weekday - current_weekday
                
                # If weeks was specified, we're already in the target week
                # Find the weekday in that week (could be before or after current day)
                if weeks is not None:
                    # In the target week, find the specific weekday
                    if days_ahead < 0:
                        days_ahead += 7
                else:
                    # No weeks specified, find next occurrence
                    if days_ahead <= 0:
                        days_ahead += 7
                
                target_date += timedelta(days=days_ahead)
            
            # 6. Handle 'dd/mm' date format (legacy format)
            if date_param is not None and '/' in str(date_param):
                parts = str(date_param).split('/')
                if len(parts) == 2:
                    try:
                        day = int(parts[0])
                        month = int(parts[1])
                        current_year = base_date.year
                        
                        candidate = base_date.replace(year=current_year, month=month, day=day)
                        
                        if candidate.date() < base_date.date():
                            candidate = candidate.replace(year=current_year + 1)
                        
                        target_date = candidate
                    except ValueError as e:
                        return [TextContent(
                            type="text",
                            text=json.dumps({
                                "error": f"Invalid date format '{date_param}'. Expected 'dd/mm'. {str(e)}"
                            })
                        )]
            
            # Convert to 10-digit Unix timestamp
            unix_timestamp = int(target_date.timestamp())
            
            # Return JSON with timestamp and human-readable format
            result = {
                "timestamp": unix_timestamp,
                "datetime": target_date.strftime("%Y-%m-%d %H:%M:%S %Z"),
                "timezone": timezone,
                "iso_format": target_date.isoformat()
            }
            
            return [TextContent(type="text", text=json.dumps(result, indent=2))]
            
        except Exception as e:
            return [TextContent(
                type="text",
                text=json.dumps({"error": f"Error processing datetime: {str(e)}"})
            )]
    
    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

async def main():
    """Run the server using stdio transport."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
