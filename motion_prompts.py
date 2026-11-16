"""Shared identity-image and motion-video instructions; no media or API access."""
from __future__ import annotations


def character_motion_prompt(*, character_name='', material_group='unknown', wardrobe_details='',
                            additional_motion='', duration_seconds=None, aspect_ratio=None):
    """Describe reference roles without inventing actions unseen in the video.

    Image identity and clothes remain fixed; the video supplies pose, timing and
    camera behavior. Duration/aspect can be omitted while preparing a website
    plan, then selected separately in the generation parameters.
    """
    character_name = str(character_name or '').strip()[:200]
    wardrobe_details = str(wardrobe_details or '').strip()[:1000]
    additional_motion = str(additional_motion or '').strip()[:6000]
    if duration_seconds is not None and (type(duration_seconds) is not int or not 1 <= duration_seconds <= 120):
        raise ValueError('duration_seconds must be an integer from 1 to 120')
    if aspect_ratio is not None and aspect_ratio not in ('9:16', '16:9', '1:1', '4:3', '3:4', '21:9'):
        raise ValueError('unsupported prompt aspect ratio')
    role = 'one male character' if material_group == 'male' else 'one female character' if material_group == 'female' else 'one character'
    actor = role + (', ' + character_name if character_name else '')
    length = str(duration_seconds) + '-second ' if duration_seconds is not None else ''
    framing = ('in ' + aspect_ratio + ' format' if aspect_ratio else 'using the requested output aspect ratio')
    paragraphs = [
        'Create a ' + length + 'video ' + framing + ' with ' + actor + '. Use the two supplied references for distinct roles.',
        'IMAGE reference defines the character identity and wardrobe only. Preserve the same face, facial structure, '
        'hair, gender presentation, body proportions, clothing fit, colors and accessories throughout. '
        'Do not copy the image\'s still pose as the animation, and do not freeze the character in that pose.',
    ]
    if wardrobe_details:
        paragraphs.append('Visible wardrobe details to retain from the image: ' + wardrobe_details + '.')
    paragraphs += [
        'VIDEO reference supplies the actual motion source, timing, rhythm, pose changes, framing and camera movement. '
        'Animate the image character through the visible gesture/dance sequence in the video; do not merely borrow its mood. '
        'Follow the observed order, directions, relative movement size, hand shapes, wrist angles, arm paths, pauses and '
        'any visible body or facial movement. Transfer only what is visible. Do not invent a second-by-second action '
        'schedule, extra choreography, hidden gestures or new camera moves.',
        ('Use the opening ' + str(duration_seconds) + ' seconds of the video reference, retaining its visible action order '
         'and pacing rather than looping gestures or adding a closing pose.' if duration_seconds is not None else
         'Use the opening portion of the video reference needed for the requested clip length, preserving its visible '
         'action order and pacing rather than inventing new beats.'),
        'Keep the image character\'s identity and outfit when transferring motion. Do not copy the video performer\'s face, '
        'hair, clothing, accessories or gender presentation. Follow the video\'s camera behavior without importing player '
        'controls, captions, watermarks or other on-screen graphics.',
        'Hands and fingers must remain anatomically plausible and consistent through motion, with natural wrist and '
        'knuckle articulation, stable finger shapes, and believable contact and occlusion. Avoid extra, missing or fused '
        'fingers, warped arms, duplicate hands, flicker and motion trails.',
    ]
    if additional_motion:
        paragraphs.append('Additional motion preferences, subordinate to the reference roles above: ' + additional_motion)
    paragraphs.append('Keep exactly one character. No added people or duplicate bodies. No outfit or makeup transformation, '
                      'costume reveal, identity morph, hairstyle change or new accessories. Keep the source-image look for '
                      'the entire clip. No added text or logos. Do not generate audio.')
    return '\n\n'.join(paragraphs)
