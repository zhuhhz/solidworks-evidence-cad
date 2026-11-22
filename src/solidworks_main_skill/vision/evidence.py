"""B009 bounded FeatureEvidence inference from a B008 ProjectionGraph candidate.

This module has no dataset/truth imports. It emits geometric support with
``feature_id=None``; final CAD feature ownership is deliberately out of scope.
"""
from dataclasses import fields
from hashlib import sha256
import json
import math
from pathlib import Path
from itertools import combinations

from experiments.three_view_reconstruction.schemas.feature_evidence import FeatureEvidence
from experiments.three_view_reconstruction.schemas.projection_graph import ProjectionGraph
from experiments.three_view_reconstruction.schemas.view_geometry import ViewGeometry, LineSegment, Circle, Arc
from experiments.three_view_reconstruction.validation.primitive_matcher import POSITION_TOLERANCE_MM, RADIUS_TOLERANCE_MM

VERSION='b009.visual-evidence-0.1'
PROJECTION_VERSION='b008.raster-0.3'


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _identity(kind,payload):
    return 'visual-evidence.'+sha256(_canonical([kind,payload]).encode()).hexdigest()[:24]


def _close(left,right,tolerance=POSITION_TOLERANCE_MM):
    return abs(float(left)-float(right))<=tolerance


def _clustered(values):
    groups=[]
    for value in sorted(float(item) for item in values):
        if not groups or value-groups[-1][-1]>POSITION_TOLERANCE_MM:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group)/len(group) for group in groups]


def _coordinate_index(value,coordinates):
    matches=[index for index,item in enumerate(coordinates) if _close(value,item)]
    if len(matches)!=1:
        raise ValueError('profile coordinate is ambiguous')
    return matches[0]


def _closed_profiles(view_name,lines):
    """Return bounded faces in an axis-aligned visible-line arrangement."""
    segments=[];points=[]
    for line in lines:
        x1,y1,x2,y2=(float(line[key]) for key in ('x1','y1','x2','y2'))
        if _close(x1,x2) and not _close(y1,y2):
            segment={'axis':'Y','fixed':(x1+x2)/2,'lo':min(y1,y2),'hi':max(y1,y2),
                     'primitive_id':line['primitive_id']}
        elif _close(y1,y2) and not _close(x1,x2):
            segment={'axis':'X','fixed':(y1+y2)/2,'lo':min(x1,x2),'hi':max(x1,x2),
                     'primitive_id':line['primitive_id']}
        else:
            raise ValueError('visible profile contains a non-axis-aligned or zero-length edge')
        segments.append(segment);points.extend(((x1,y1),(x2,y2)))
    for horizontal in (row for row in segments if row['axis']=='X'):
        for vertical in (row for row in segments if row['axis']=='Y'):
            if (horizontal['lo']-POSITION_TOLERANCE_MM<=vertical['fixed']<=horizontal['hi']+POSITION_TOLERANCE_MM
                    and vertical['lo']-POSITION_TOLERANCE_MM<=horizontal['fixed']<=vertical['hi']+POSITION_TOLERANCE_MM):
                points.append((vertical['fixed'],horizontal['fixed']))
    xs=_clustered(point[0] for point in points);ys=_clustered(point[1] for point in points)
    nodes={(_coordinate_index(x,xs),_coordinate_index(y,ys)) for x,y in points}
    adjacency={node:set() for node in nodes};edge_sources={}
    for segment in segments:
        if segment['axis']=='X':
            fixed=_coordinate_index(segment['fixed'],ys)
            candidates=sorted((node for node in nodes if node[1]==fixed
                               and segment['lo']-POSITION_TOLERANCE_MM<=xs[node[0]]<=segment['hi']+POSITION_TOLERANCE_MM),
                              key=lambda node:node[0])
        else:
            fixed=_coordinate_index(segment['fixed'],xs)
            candidates=sorted((node for node in nodes if node[0]==fixed
                               and segment['lo']-POSITION_TOLERANCE_MM<=ys[node[1]]<=segment['hi']+POSITION_TOLERANCE_MM),
                              key=lambda node:node[1])
        if len(candidates)<2:
            raise ValueError('visible profile edge has no resolvable endpoints')
        for first,second in zip(candidates,candidates[1:]):
            edge=frozenset((first,second))
            if edge in edge_sources:
                raise ValueError('overlapping visible profile edges are ambiguous')
            edge_sources[edge]=segment['primitive_id']
            adjacency[first].add(second);adjacency[second].add(first)
    visited=set();faces=[]
    for edge in sorted(edge_sources,key=lambda item:sorted(item)):
        first,second=sorted(edge)
        for start in ((first,second),(second,first)):
            if start in visited:
                continue
            current=start;face=[]
            while current not in visited:
                visited.add(current);left,right=current;face.append(left)
                neighbours=sorted(adjacency[right],key=lambda node:math.atan2(ys[node[1]]-ys[right[1]],
                                                                              xs[node[0]]-xs[right[0]]))
                if left not in neighbours:
                    raise ValueError('visible profile graph is inconsistent')
                following=neighbours[(neighbours.index(left)-1)%len(neighbours)]
                current=(right,following)
            if current!=start:
                raise ValueError('visible profile traversal is ambiguous')
            coordinates=[(xs[node[0]],ys[node[1]]) for node in face]
            area=sum(x1*y2-x2*y1 for (x1,y1),(x2,y2) in
                     zip(coordinates,coordinates[1:]+coordinates[:1]))/2
            if area>POSITION_TOLERANCE_MM**2:
                sources=sorted({edge_sources[frozenset((face[index],face[(index+1)%len(face)]))]
                                for index in range(len(face))})
                bounds=[min(point[0] for point in coordinates),min(point[1] for point in coordinates),
                        max(point[0] for point in coordinates),max(point[1] for point in coordinates)]
                profile_id=_identity('VISIBLE_PROFILE',[view_name,sources])
                faces.append({'primitive_id':profile_id,'geometry_type':'VISIBLE_PROFILE','view':view_name,
                              'source_primitive_ids':sources,'bounds_mm':bounds,
                              'relationship':'CLOSED_VISIBLE_BOUNDARY'})
    if not faces:
        raise ValueError('visible profile geometry has no closed bounded face')
    identities=[face['primitive_id'] for face in faces]
    if len(set(identities))!=len(identities):
        raise ValueError('visible profile identities are ambiguous')
    return sorted(faces,key=lambda row:row['primitive_id'])


def _arc_endpoint(arc,angle):
    radians=math.radians(float(angle))
    return (float(arc['x'])+float(arc['radius'])*math.cos(radians),
            float(arc['y'])+float(arc['radius'])*math.sin(radians))


def _angle_on_arc(angle,arc):
    start=float(arc['start_angle_deg'])%360;end=float(arc['end_angle_deg'])%360;angle%=360
    if arc['sweep_direction']=='CCW':
        return (angle-start)%360<=(end-start)%360+1e-9
    return (start-angle)%360<=(start-end)%360+1e-9


def _closed_line_arc_boundary(view_name,lines,arcs):
    """Build one closed, tangent mixed boundary without using element order."""
    elements={}
    for line in lines:
        elements[line['primitive_id']]={'kind':'LINE','value':line,
            'endpoints':[(float(line['x1']),float(line['y1'])),(float(line['x2']),float(line['y2']))]}
    for arc in arcs:
        elements[arc['primitive_id']]={'kind':'ARC','value':arc,
            'endpoints':[_arc_endpoint(arc,arc['start_angle_deg']),_arc_endpoint(arc,arc['end_angle_deg'])]}
    if not lines or not arcs:
        raise ValueError('mixed boundary requires observable LINE and ARC elements')
    endpoints=[(identity,index,point) for identity,row in elements.items()
               for index,point in enumerate(row['endpoints'])]
    links={};adjacency={identity:set() for identity in elements}
    for identity,index,point in endpoints:
        matches=[(other_identity,other_index,other_point)
                 for other_identity,other_index,other_point in endpoints
                 if other_identity!=identity and math.dist(point,other_point)<=POSITION_TOLERANCE_MM]
        if len(matches)!=1:
            raise ValueError('LINE/ARC boundary endpoint does not close uniquely')
        other_identity,other_index,_=matches[0]
        links[(identity,index)]=(other_identity,other_index)
        adjacency[identity].add(other_identity)
    reached={min(elements)};changed=True
    while changed:
        changed=False
        for identity in tuple(reached):
            if not adjacency[identity]<=reached:
                reached.update(adjacency[identity]);changed=True
    if reached!=set(elements) or any(len(neighbours)!=2 for neighbours in adjacency.values()):
        raise ValueError('LINE/ARC boundary is disconnected or ambiguous')
    for (identity,index),(other_identity,_) in links.items():
        row=elements[identity];other=elements[other_identity]
        if row['kind']==other['kind']:
            raise ValueError('LINE/ARC boundary junction must connect unlike element types')
        arc_row=row if row['kind']=='ARC' else other
        line_row=other if row['kind']=='ARC' else row
        point=row['endpoints'][index]
        arc=arc_row['value'];line=line_row['value']
        radial=(point[0]-float(arc['x']),point[1]-float(arc['y']))
        direction=(float(line['x2'])-float(line['x1']),float(line['y2'])-float(line['y1']))
        denominator=math.hypot(*radial)*math.hypot(*direction)
        if not denominator or abs(radial[0]*direction[0]+radial[1]*direction[1])/denominator \
                >POSITION_TOLERANCE_MM/max(float(arc['radius']),POSITION_TOLERANCE_MM):
            raise ValueError('LINE/ARC boundary is not tangent at a junction')
    points=[point for row in elements.values() for point in row['endpoints']]
    for arc in arcs:
        points.extend(_arc_endpoint(arc,angle) for angle in (0,90,180,270) if _angle_on_arc(angle,arc))
    bounds=[min(point[0] for point in points),min(point[1] for point in points),
            max(point[0] for point in points),max(point[1] for point in points)]
    line_ids=sorted(line['primitive_id'] for line in lines);arc_ids=sorted(arc['primitive_id'] for arc in arcs)
    identity=_identity('LINE_ARC_PROFILE',[view_name,line_ids,arc_ids])
    return {'primitive_id':identity,'geometry_type':'LINE_ARC_PROFILE','view':view_name,
            'source_primitive_ids':sorted(line_ids+arc_ids),'line_primitive_ids':line_ids,
            'arc_primitive_ids':arc_ids,'bounds_mm':bounds,
            'relationship':'CLOSED_TANGENT_LINE_ARC_BOUNDARY'}


def _normalized_projection_graph(graph):
    payload=json.loads(_canonical(graph))
    for view_name in ('front','top','left'):
        for field in ('visible_segments','hidden_segments','circles','arcs','centerlines','hidden_line_pairs'):
            payload[view_name][field]=sorted(payload[view_name][field],key=lambda row:row.get('primitive_id',''))
    payload['feature_evidence_records']=sorted(payload['feature_evidence_records'],key=lambda row:_canonical(row))
    payload['coordinate_convention']['relations']=sorted(payload['coordinate_convention']['relations'])
    return payload


def _load_json(path):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:
                raise ValueError('duplicate JSON key: '+key)
            result[key]=value
        return result
    def constant(value):
        raise ValueError('non-finite JSON constant: '+value)
    value=json.loads(Path(path).read_text(encoding='utf-8-sig'),object_pairs_hook=pairs,parse_constant=constant)
    if not isinstance(value,dict):
        raise ValueError('ProjectionGraph candidate JSON must be an object')
    return value


def infer(projection_candidate):
    """Infer bounded B009 profile/circle/hidden-pair evidence.

    Supported geometry is discovered from the candidate: closed axis-aligned
    visible profiles, one circle, and two full-span hidden-line pairs aligned
    through the public coordinate convention. No expected IDs, counts,
    dimensions, filenames, feature topology, or truth data are inputs.
    """
    result={'schema_version':'b009.feature-evidence-candidate/0.1','version':VERSION,
            'status':'UNSUPPORTED_FEATURE','projection_graph_sha256':None,
            'feature_evidence':[],'derived_primitives':[],'cross_view_relationships':[],
            'checks':{'unresolved_reference_count':0,'unknown_count':0,'unattributed_count':0,
                      'feature_ownership':'NOT_INFERRED_BY_SCOPE'},
            'issues':[]}
    try:
        expected_envelope={'version','image_sha256','image_size_px','dpi','layout','pixel_origin','pixel_sample',
                           'pixel_axes','scale_numerator','scale_denominator','detections','issues',
                           'projection_graph','status','view_transforms'}
        if not isinstance(projection_candidate,dict) or set(projection_candidate)!=expected_envelope:
            raise ValueError('exact visual ProjectionGraph candidate fields required')
        if (projection_candidate.get('version')!=PROJECTION_VERSION
                or projection_candidate.get('status')!='EXTRACTED' or projection_candidate.get('issues')!=[]):
            raise ValueError('candidate extraction must complete without issues')
        graph=projection_candidate.get('projection_graph')
        if not isinstance(graph,dict) or set(graph)!={field.name for field in fields(ProjectionGraph)}:
            raise ValueError('exact ProjectionGraph candidate fields required')
        if graph['feature_evidence_records']!=[] or any(graph[key] is not None for key in
                ('feature_evidence','expected_features','center_requirements','reference_integrity')):
            raise ValueError('semantic ProjectionGraph content is not accepted')
        convention=graph['coordinate_convention']
        if (graph['projection']!='third_angle' or convention.get('origin')!='VIEW_BOTTOM_LEFT'
                or convention.get('units')!='mm'):
            raise ValueError('unsupported projection coordinate convention')
        inventory={};views={};profile_boundaries=[];circles=[];arcs=[];centerlines=[]
        for view_name in ('front','top','left'):
            view=graph[view_name]
            if not isinstance(view,dict) or set(view)!={field.name for field in fields(ViewGeometry)} or view['name']!=view_name:
                raise ValueError('invalid visual view schema')
            if view['hidden_line_pairs']:
                raise ValueError('bounded evidence rule does not accept pre-grouped hidden pairs')
            width,height=view['horizontal_extent'],view['vertical_extent']
            if any(type(value) not in (int,float) or not math.isfinite(value) or value<=0 for value in (width,height)):
                raise ValueError('invalid view extent')
            for field,kind,cls in (('visible_segments','LINE',LineSegment),
                                   ('hidden_segments','HIDDEN_LINE',LineSegment),('circles','CIRCLE',Circle),
                                   ('arcs','ARC',Arc),('centerlines','CENTERLINE',LineSegment)):
                if not isinstance(view[field],list):
                    raise ValueError('primitive collection must be a list')
                for primitive in view[field]:
                    if set(primitive)!={item.name for item in fields(cls)}:
                        raise ValueError('invalid primitive schema')
                    primitive_id=primitive['primitive_id']
                    if not isinstance(primitive_id,str) or not primitive_id or primitive_id in inventory:
                        raise ValueError('duplicate or empty primitive identity')
                    numeric={item.name for item in fields(cls)}-{'primitive_id','sweep_direction'}
                    if any(type(primitive[key]) not in (int,float) or not math.isfinite(primitive[key]) for key in numeric):
                        raise ValueError('non-finite primitive geometry')
                    inventory[primitive_id]=(view_name,kind,primitive)
                    if kind=='CIRCLE':
                        if primitive['diameter']<=0:
                            raise ValueError('invalid observed circle')
                        circles.append((view_name,primitive))
                    elif kind=='ARC':
                        Arc(**primitive);arcs.append((view_name,primitive))
                    elif kind=='CENTERLINE':
                        centerlines.append((view_name,primitive))
            profile_boundaries.extend(_closed_profiles(view_name,view['visible_segments']))
            views[view_name]=view
        detections=projection_candidate['detections']
        if not isinstance(detections,list):
            raise ValueError('visual detection provenance must be a list')
        detection_inventory={}
        for detection in detections:
            if not isinstance(detection,dict) or set(detection)!={'primitive_id','view','primitive_type',
                                                                  'raw_image_geometry','normalized_geometry'}:
                raise ValueError('invalid visual detection provenance')
            primitive_id=detection['primitive_id']
            if primitive_id in detection_inventory or primitive_id not in inventory:
                raise ValueError('duplicate or unresolved visual detection provenance')
            view,kind,primitive=inventory[primitive_id]
            geometry={key:value for key,value in primitive.items() if key!='primitive_id'}
            if (detection['view'],detection['primitive_type'])!=(view,kind) or detection['normalized_geometry']!=geometry:
                raise ValueError('visual detection provenance disagrees with ProjectionGraph geometry')
            if not isinstance(detection['raw_image_geometry'],dict):
                raise ValueError('raw image provenance is required')
            detection_inventory[primitive_id]=detection
        if set(detection_inventory)!=set(inventory):
            raise ValueError('visual detection provenance does not cover every primitive')
        axes={name:convention.get(name,'').split('/') for name in views}
        if any(len(value)!=2 for value in axes.values()):
            raise ValueError('invalid public view-axis mapping')
        expected_relations=set()
        for left,right in combinations(views,2):
            shared=set(axes[left])&set(axes[right])
            if len(shared)!=1:
                raise ValueError('view pair has no unique shared coordinate')
            coordinate=shared.pop().lower()
            expected_relations.add(frozenset(((left,coordinate),(right,coordinate))))
        actual_relations=set()
        for relation in convention.get('relations',[]):
            sides=str(relation).split('=')
            try:
                parsed=frozenset(tuple(side.split('.')) for side in sides)
            except (TypeError,ValueError) as exc:
                raise ValueError('malformed coordinate relation') from exc
            if len(sides)!=2 or any(len(side)!=2 for side in parsed):
                raise ValueError('malformed coordinate relation')
            actual_relations.add(parsed)
        if actual_relations!=expected_relations:
            raise ValueError('ProjectionGraph coordinate relations are incomplete or inconsistent')
        profile_line_ids={source for profile in profile_boundaries for source in profile['source_primitive_ids']}
        residual={view_name:[line for line in view['visible_segments']
                             if line['primitive_id'] not in profile_line_ids]
                  for view_name,view in views.items()}
        line_arc_boundaries=[]
        if arcs:
            if circles:
                raise ValueError('circle and LINE/ARC boundary observations are ambiguous')
            boundary_views={view for view,_ in arcs}|{view for view,lines in residual.items() if lines}
            if len(boundary_views)!=1:
                raise ValueError('ARC grouping is ambiguous across views')
            feature_view=boundary_views.pop()
            if any(view!=feature_view and lines for view,lines in residual.items()):
                raise ValueError('unresolved visible profile edges remain')
            boundary=_closed_line_arc_boundary(feature_view,residual[feature_view],
                                               [arc for view,arc in arcs if view==feature_view])
            bounds=boundary.pop('bounds_mm');view_axes=axes[feature_view]
            boundary['coordinate_intervals']={view_axes[0].lower():bounds[0::2],
                                              view_axes[1].lower():bounds[1::2]}
            line_arc_boundaries.append(boundary)
            feature_intervals=boundary['coordinate_intervals'];feature_kind='LINE_ARC_PROFILE'
            if views[feature_view]['hidden_segments']:
                raise ValueError('LINE/ARC boundary view has conflicting hidden support')
        else:
            if any(residual.values()):
                raise ValueError('visible profile edges remain outside a closed boundary')
            if len(circles)!=1:
                raise ValueError('bounded rule requires one unambiguous circle or LINE/ARC boundary')
            feature_view,circle=circles[0];radius=float(circle['diameter'])/2;view_axes=axes[feature_view]
            feature_intervals={view_axes[0].lower():[float(circle['x'])-radius,float(circle['x'])+radius],
                               view_axes[1].lower():[float(circle['y'])-radius,float(circle['y'])+radius]}
            feature_kind='CIRCLE'
            if views[feature_view]['hidden_segments'] or centerlines:
                raise ValueError('circle evidence has conflicting hidden or centerline support')
        pairs=[]
        for view_name in views:
            if view_name==feature_view:
                continue
            shared=set(axes[feature_view])&set(axes[view_name])
            if len(shared)!=1:
                raise ValueError('orthogonal view has no unique shared coordinate')
            coordinate=shared.pop()
            view_index=axes[view_name].index(coordinate)
            lines=views[view_name]['hidden_segments']
            if len(lines)!=2:
                raise ValueError('orthogonal support must contain exactly two hidden lines')
            offsets=[]
            for line in lines:
                if view_index==0:
                    if not _close(line['x1'],line['x2']) or not (_close(min(line['y1'],line['y2']),0) and _close(max(line['y1'],line['y2']),views[view_name]['vertical_extent'])):
                        raise ValueError('hidden support is not a full-span projected line')
                    offsets.append(float((line['x1']+line['x2'])/2))
                else:
                    if not _close(line['y1'],line['y2']) or not (_close(min(line['x1'],line['x2']),0) and _close(max(line['x1'],line['x2']),views[view_name]['horizontal_extent'])):
                        raise ValueError('hidden support is not a full-span projected line')
                    offsets.append(float((line['y1']+line['y2'])/2))
            offsets.sort()
            interval=feature_intervals[coordinate.lower()]
            if not all(_close(left,right,max(POSITION_TOLERANCE_MM,RADIUS_TOLERANCE_MM))
                       for left,right in zip(offsets,interval)):
                raise ValueError('observed boundary and hidden-line support are not cross-view aligned')
            source_ids=sorted(line['primitive_id'] for line in lines)
            pair_id=_identity('HIDDEN_LINE_PAIR',[view_name,source_ids])
            pairs.append({'primitive_id':pair_id,'geometry_type':'HIDDEN_LINE_PAIR','view':view_name,
                          'source_primitive_ids':source_ids,'shared_coordinate':coordinate,
                          'offsets_mm':offsets,'relationship':'PARALLEL_FULL_SPAN_MATCHED_TO_BOUNDARY_EXTREMA'})
        if feature_kind=='LINE_ARC_PROFILE':
            if len(centerlines)!=len(views) or {view for view,_ in centerlines}!=set(views):
                raise ValueError('LINE/ARC boundary requires one CENTERLINE observation per view')
            for view_name,line in centerlines:
                x1,y1,x2,y2=(float(line[key]) for key in ('x1','y1','x2','y2'))
                horizontal=_close(y1,y2) and not _close(x1,x2)
                vertical=_close(x1,x2) and not _close(y1,y2)
                if not (horizontal or vertical):
                    raise ValueError('CENTERLINE must be one nonzero axis-aligned observation')
                line_axis=axes[view_name][0 if horizontal else 1].lower()
                fixed_axis=axes[view_name][1 if horizontal else 0].lower()
                span=[min(x1,x2),max(x1,x2)] if horizontal else [min(y1,y2),max(y1,y2)]
                fixed=(y1+y2)/2 if horizontal else (x1+x2)/2
                if view_name==feature_view:
                    expected=feature_intervals[fixed_axis]
                    along=feature_intervals[line_axis]
                    if (not _close(fixed,sum(expected)/2)
                            or span[0]>along[0]+POSITION_TOLERANCE_MM
                            or span[1]<along[1]-POSITION_TOLERANCE_MM):
                        raise ValueError('CENTERLINE is inconsistent with LINE/ARC boundary')
                else:
                    shared=(set(axes[feature_view])&set(axes[view_name])).pop().lower()
                    expected=feature_intervals[shared]
                    extent=views[view_name]['horizontal_extent' if horizontal else 'vertical_extent']
                    if (fixed_axis!=shared or not _close(fixed,sum(expected)/2)
                            or not (_close(span[0],0) and _close(span[1],extent))):
                        raise ValueError('CENTERLINE is inconsistent with orthogonal hidden support')
        for profile in profile_boundaries:
            bounds=profile.pop('bounds_mm');view_axes=axes[profile['view']]
            profile['coordinate_intervals']={view_axes[0].lower():bounds[0::2],
                                             view_axes[1].lower():bounds[1::2]}
        profile_edges=set()
        for left_view,right_view in combinations(views,2):
            shared=(set(axes[left_view])&set(axes[right_view])).pop().lower()
            left_profiles=[row for row in profile_boundaries if row['view']==left_view]
            right_profiles=[row for row in profile_boundaries if row['view']==right_view]
            for left_profile in left_profiles:
                interval=left_profile['coordinate_intervals'][shared]
                matches=[right_profile for right_profile in right_profiles
                         if all(_close(a,b) for a,b in zip(interval,right_profile['coordinate_intervals'][shared]))]
                if len(matches)!=1:
                    raise ValueError('profile correspondence is ambiguous across '+left_view+'/'+right_view)
                profile_edges.add(frozenset((left_profile['primitive_id'],matches[0]['primitive_id'])))
            for right_profile in right_profiles:
                interval=right_profile['coordinate_intervals'][shared]
                matches=[left_profile for left_profile in left_profiles
                         if all(_close(a,b) for a,b in zip(interval,left_profile['coordinate_intervals'][shared]))]
                if len(matches)!=1:
                    raise ValueError('profile correspondence is ambiguous across '+left_view+'/'+right_view)
        profile_by_id={row['primitive_id']:row for row in profile_boundaries}
        pending=set(profile_by_id);profile_groups=[]
        while pending:
            component={min(pending)};changed=True
            while changed:
                changed=False
                for edge in profile_edges:
                    if component&set(edge) and not set(edge)<=component:
                        component.update(edge);changed=True
            pending-=component
            if ({profile_by_id[item]['view'] for item in component}!=set(views)
                    or len(component)!=len(views)
                    or any(frozenset(pair) not in profile_edges for pair in combinations(component,2))):
                raise ValueError('cross-view profile correspondence is incomplete')
            profile_groups.append(sorted(component))
        records=[]; roles={}
        boundary_lines={source for boundary in line_arc_boundaries for source in boundary['line_primitive_ids']}
        for primitive_id,(view,kind,_) in inventory.items():
            if kind not in {'LINE','CIRCLE','ARC','CENTERLINE'}:
                continue
            if kind=='LINE':
                role='LINE_ARC_BOUNDARY_EDGE' if primitive_id in boundary_lines else 'VISIBLE_PROFILE_EDGE'
            else:
                role={'CIRCLE':'OBSERVED_CIRCLE','ARC':'ARC_BOUNDARY',
                      'CENTERLINE':'CENTERLINE_SUPPORT'}[kind]
            evidence_id=_identity(role,primitive_id)
            records.append(FeatureEvidence(None,evidence_id,view,kind,primitive_id,1.0,'VISUAL_GEOMETRY_RULE').to_dict())
            roles[evidence_id]=role
        for pair in pairs:
            evidence_id=_identity('ORTHOGONAL_HIDDEN_PAIR',pair['primitive_id'])
            records.append(FeatureEvidence(None,evidence_id,pair['view'],'HIDDEN_LINE_PAIR',
                                           pair['primitive_id'],1.0,'VISUAL_GEOMETRY_RULE').to_dict())
            roles[evidence_id]='ORTHOGONAL_HIDDEN_PAIR'
        records.sort(key=lambda row:row['evidence_id']);pairs.sort(key=lambda row:row['primitive_id'])
        covered={row['geometry_reference'] for row in records if row['geometry_type']!='HIDDEN_LINE_PAIR'}
        covered.update(source for pair in pairs for source in pair['source_primitive_ids'])
        if covered!=set(inventory):
            raise ValueError('candidate geometry is not completely covered by evidence')
        evidence_by_geometry={row['geometry_reference']:row['evidence_id'] for row in records}
        relationships=[]
        if feature_kind=='CIRCLE':
            relationship_sources=sorted(row['evidence_id'] for row in records
                                         if roles[row['evidence_id']]!='VISIBLE_PROFILE_EDGE')
            relationships.append({'relationship_id':_identity('CIRCLE_ORTHOGONAL_SUPPORT',relationship_sources),
                                  'evidence_type':'CIRCLE_ORTHOGONAL_HIDDEN_PAIR_SUPPORT',
                                  'source_evidence_ids':relationship_sources,
                                  'relationship':'SHARED_CENTER_AND_EQUAL_HALF_SEPARATION',
                                  'confidence':1.0,'source':'VISUAL_GEOMETRY_RULE'})
        else:
            boundary=line_arc_boundaries[0]
            boundary_sources=sorted(evidence_by_geometry[source] for source in boundary['source_primitive_ids'])
            relationships.append({'relationship_id':_identity('CLOSED_LINE_ARC_BOUNDARY',boundary_sources),
                                  'evidence_type':'CLOSED_LINE_ARC_BOUNDARY',
                                  'source_evidence_ids':boundary_sources,
                                  'source_boundary_id':boundary['primitive_id'],
                                  'relationship':'ENDPOINT_CLOSED_AND_TANGENT',
                                  'confidence':1.0,'source':'VISUAL_GEOMETRY_RULE'})
            support_sources=sorted(row['evidence_id'] for row in records
                                   if roles[row['evidence_id']]!='VISIBLE_PROFILE_EDGE')
            support_ids=sorted([boundary['primitive_id']]+[pair['primitive_id'] for pair in pairs])
            relationships.append({'relationship_id':_identity('LINE_ARC_ORTHOGONAL_SUPPORT',support_ids),
                                  'evidence_type':'LINE_ARC_BOUNDARY_ORTHOGONAL_SUPPORT',
                                  'source_evidence_ids':support_sources,'source_derived_ids':support_ids,
                                  'relationship':'SHARED_EXTREMA_AND_CENTERLINES',
                                  'confidence':1.0,'source':'VISUAL_GEOMETRY_RULE'})
        for group in profile_groups:
            sources=sorted({evidence_by_geometry[source] for profile_id in group
                            for source in profile_by_id[profile_id]['source_primitive_ids']})
            relationships.append({'relationship_id':_identity('ORTHOGONAL_PROFILE_CORRESPONDENCE',group),
                                  'evidence_type':'ORTHOGONAL_PROFILE_CORRESPONDENCE',
                                  'source_evidence_ids':sources,'source_profile_ids':group,
                                  'relationship':'EQUAL_SHARED_COORDINATE_INTERVALS',
                                  'confidence':1.0,'source':'VISUAL_GEOMETRY_RULE'})
        relationships.sort(key=lambda row:row['relationship_id'])
        derived=sorted(pairs+profile_boundaries+line_arc_boundaries,key=lambda row:row['primitive_id'])
        graph_hash=sha256(_canonical(_normalized_projection_graph(graph)).encode()).hexdigest()
        result.update(status='INFERRED',projection_graph_sha256=graph_hash,
                      feature_evidence=records,derived_primitives=derived,
                      cross_view_relationships=relationships,roles=roles)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
        result['issues'].append({'reason':str(exc)})
    return result


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projection-candidate',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        candidate=_load_json(args.projection_candidate)
        result=infer(candidate)
        target=Path(args.output);target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(json.dumps(result,indent=2,sort_keys=True,ensure_ascii=False,allow_nan=False)+'\n')
    except (OSError,json.JSONDecodeError,ValueError) as exc:
        parser.exit(2,'evidence: '+str(exc)+'\n')


if __name__=='__main__':
    main()
